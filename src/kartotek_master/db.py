"""
Couche d'accès SQLite « brute » (sqlite3 stdlib, pas de SQLAlchemy).

Choix de conception, pensés pour la robustesse et une faible empreinte mémoire :
- une connexion SQLite par thread (sqlite3 n'aime pas le partage de connexion
  entre threads), ouverte à la demande et fermée immédiatement après usage ;
- mode WAL pour permettre des lectures concurrentes pendant les écritures
  du "poller" ;
- écritures par lots (executemany + une seule transaction) pour éviter les
  milliers de commits individuels lors d'une synchronisation ;
- les requêtes de la carte ne renvoient jamais la table entière : elles sont
  bornées par une bounding box + une limite stricte du nombre de lignes, et
  regroupent les points en clusters (agrégation SQL) quand on est dézoomé.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_local = threading.local()
_db_path = None
_init_lock = threading.Lock()


def configure(path: str):
    """Doit être appelé une fois au démarrage avec le chemin du fichier .db"""
    global _db_path
    _db_path = path
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)


def _get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_db_path, timeout=30, check_same_thread=True)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


@contextmanager
def get_cursor(commit: bool = False):
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    finally:
        cur.close()


def init_db():
    """Crée le schéma s'il n'existe pas encore. Idempotent."""
    with _init_lock:
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS servers_state (
                    server_url   TEXT PRIMARY KEY,
                    name         TEXT,
                    dbid         TEXT,
                    last_check   TEXT,
                    last_sync    TEXT,
                    points_count INTEGER NOT NULL DEFAULT 0,
                    last_error   TEXT,
                    bounds_min_lat   REAL,
                    bounds_max_lat   REAL,
                    bounds_min_lon   REAL,
                    bounds_max_lon   REAL,
                    bounds_updated_at TEXT
                );
                """
            )
            # Migration douce : si la base existait déjà avant l'ajout de `name`.
            try:
                cur.execute("ALTER TABLE servers_state ADD COLUMN name TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            # Migration douce : idem pour `description` (texte libre saisi
            # dans servers.json, affiché sur la page /info/serveurs/).
            try:
                cur.execute("ALTER TABLE servers_state ADD COLUMN description TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            # Migration douce : idem pour les colonnes de bounding box
            # (étendue géographique de la collection de chaque serveur,
            # récupérée via GET /api/v1/bounds).
            for column in (
                "bounds_min_lat", "bounds_max_lat", "bounds_min_lon", "bounds_max_lon",
            ):
                try:
                    cur.execute(f"ALTER TABLE servers_state ADD COLUMN {column} REAL;")
                except sqlite3.OperationalError:
                    pass  # la colonne existe déjà
            # Migration douce : cache des /api/v1/capabilities de chaque
            # serveur (voir update_server_capabilities), pour que
            # /api/v1/servers puisse les exposer sans appel HTTP à la
            # demande — voir poller._fetch_capabilities.
            try:
                cur.execute("ALTER TABLE servers_state ADD COLUMN capabilities_json TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            try:
                cur.execute("ALTER TABLE servers_state ADD COLUMN capabilities_updated_at TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            try:
                cur.execute("ALTER TABLE servers_state ADD COLUMN bounds_updated_at TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gps_points (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_url  TEXT NOT NULL,
                    external_id TEXT,
                    lat         REAL NOT NULL,
                    lon         REAL NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            # Migration douce : si la base existait déjà avant l'ajout de
            # l'identifiant de carte renvoyé par /api/v1/gps.
            try:
                cur.execute("ALTER TABLE gps_points ADD COLUMN external_id TEXT;")
            except sqlite3.OperationalError:
                pass  # la colonne existe déjà
            # Index unique (partiel : ignore les lignes sans external_id,
            # ancien format) nécessaire à l'UPSERT de upsert_points() —
            # c'est lui qui permet de mettre à jour un point existant
            # (nouvelles coordonnées) sans réinitialiser sa colonne
            # created_at, indispensable au filtre `since` de
            # /api/v1/nearby (voir geoapi.nearby et
            # docs/07-PUSH_NOTIFICATIONS.md pour le principe similaire
            # côté push). Si l'index ne peut pas être créé (base
            # existante avec des doublons résiduels, cas qui ne devrait
            # plus se produire depuis que insert_points dédupliquait déjà
            # par page), on continue sans lui : upsert_points retombe
            # alors sur un simple INSERT (voir son commentaire).
            try:
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_gps_points_server_external "
                    "ON gps_points(server_url, external_id) WHERE external_id IS NOT NULL;"
                )
            except sqlite3.Error as exc:
                logger.warning(
                    "Impossible de créer idx_gps_points_server_external (%s) — "
                    "le filtre `since` de /api/v1/nearby restera dégradé "
                    "(created_at pourra être réinitialisé à chaque resynchronisation) "
                    "tant que cette base contiendra des doublons (server_url, external_id).",
                    exc,
                )
            # Index composites pour accélérer les filtres bbox (WHERE lat BETWEEN.. AND lon BETWEEN..)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_lat ON gps_points(lat);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_lon ON gps_points(lon);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_server ON gps_points(server_url);")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_gps_lat_lon_server ON gps_points(lat, lon, server_url);"
            )
            # Registre centralisé des tokens push (FCM/APNs) — voir
            # kartotek_master.push. Un enregistrement par appareil (clé =
            # token), mis à jour à chaque POST /api/v1/push/register.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS push_registrations (
                    token       TEXT PRIMARY KEY,
                    platform    TEXT NOT NULL,
                    lat         REAL NOT NULL,
                    lon         REAL NOT NULL,
                    radius_m    REAL NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_lat_lon ON push_registrations(lat, lon);"
            )
            # Utilisé par purge_stale_push_registrations() pour retrouver
            # rapidement les enregistrements jamais renouvelés, sans scanner
            # toute la table à chaque cycle de purge.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_updated_at ON push_registrations(updated_at);"
            )
            # Entitlements "version pro" validés côté serveur — voir
            # kartotek_master.purchases. Un achat in-app (Google Play ou
            # App Store) est vérifié une fois auprès du store correspondant
            # puis mémorisé ici, pour que l'appli mobile puisse
            # redemander son statut (device changé de serveur KartoTek,
            # réinstallation, "restore purchases") sans revalider le reçu
            # à chaque lancement. Clé = (device_id, product_id) : un même
            # appareil peut en théorie posséder plusieurs produits futurs
            # (aujourd'hui un seul, l'unlock pro).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS purchase_entitlements (
                    device_id     TEXT NOT NULL,
                    product_id    TEXT NOT NULL,
                    platform      TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    purchase_ref  TEXT NOT NULL,
                    verified_at   TEXT NOT NULL,
                    PRIMARY KEY (device_id, product_id)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_purchase_device ON purchase_entitlements(device_id);"
            )


def get_server_state(server_url: str):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM servers_state WHERE server_url = ?", (server_url,))
        row = cur.fetchone()
        return dict(row) if row else None


def touch_server_check(server_url: str, name: str = None, description: str = None, error: str = None):
    """Met à jour la date de dernière vérification (que le dbid ait changé ou non)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO servers_state (server_url, name, description, last_check, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(server_url) DO UPDATE SET
                name = COALESCE(excluded.name, servers_state.name),
                description = COALESCE(excluded.description, servers_state.description),
                last_check = excluded.last_check,
                last_error = excluded.last_error
            """,
            (server_url, name, description, now, error),
        )


def update_server_dbid(server_url: str, dbid: str, points_count: int, name: str = None, description: str = None):
    """
    points_count : nombre de points insérés lors de CETTE synchronisation
    (remplace la valeur précédente, ne s'y ajoute pas — chaque
    resynchronisation complète purge d'abord les anciens points du
    serveur, donc points_count doit refléter l'état courant de la table,
    pas un cumul depuis le début).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO servers_state (server_url, name, description, dbid, last_check, last_sync, points_count, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(server_url) DO UPDATE SET
                name = COALESCE(excluded.name, servers_state.name),
                description = COALESCE(excluded.description, servers_state.description),
                dbid = excluded.dbid,
                last_check = excluded.last_check,
                last_sync = excluded.last_sync,
                points_count = excluded.points_count,
                last_error = NULL
            """,
            (server_url, name, description, dbid, now, now, points_count),
        )


def update_server_bounds(server_url: str, min_lat: float, max_lat: float, min_lon: float, max_lon: float):
    """
    Enregistre l'étendue géographique (bounding box) de la collection d'un
    serveur, récupérée via GET /api/v1/bounds. Mis à jour à chaque cycle du
    poller, indépendamment des changements de dbid.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO servers_state (
                server_url, bounds_min_lat, bounds_max_lat,
                bounds_min_lon, bounds_max_lon, bounds_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_url) DO UPDATE SET
                bounds_min_lat = excluded.bounds_min_lat,
                bounds_max_lat = excluded.bounds_max_lat,
                bounds_min_lon = excluded.bounds_min_lon,
                bounds_max_lon = excluded.bounds_max_lon,
                bounds_updated_at = excluded.bounds_updated_at
            """,
            (server_url, min_lat, max_lat, min_lon, max_lon, now),
        )


def upsert_points(server_url: str, points):
    """
    points : itérable de (external_id, lat, lon). `external_id` (l'identifiant
    de la carte côté serveur distant) peut être `None` si non fourni.

    UPSERT (INSERT ... ON CONFLICT DO UPDATE) plutôt qu'un simple INSERT :
    si un point avec le même (server_url, external_id) existe déjà, seules
    ses coordonnées (lat/lon) sont mises à jour — sa colonne `created_at`
    (date de première apparition dans le cache du master) reste INCHANGÉE.

    C'est cette préservation qui rend possible le filtre `since` de
    /api/v1/nearby (voir geoapi.nearby, pour le geofencing en arrière-plan
    de l'appli mobile) : sans elle, la moindre resynchronisation — même
    déclenchée par l'ajout d'une seule carte ailleurs sur le même serveur,
    puisque le dbid change pour la collection entière — aurait
    réinitialisé la date de "première apparition" de TOUS les points de
    ce serveur, rendant "depuis when() a-t-on du nouveau" invérifiable.

    Les points sans `external_id` (ancien format d'API, avant l'ajout de
    l'id de carte à /api/v1/gps) ne peuvent pas être dédupliqués de façon
    fiable sur ce critère : ils sont toujours insérés tels quels, comme
    avant l'introduction de l'upsert. Idem si l'index unique nécessaire à
    l'upsert n'a pas pu être créé au démarrage (voir configure()) : dans
    ce cas de repli, tous les points sont insérés en INSERT simple.

    Insertion en une seule transaction (executemany) pour limiter les I/O
    disque, même si la page contient des centaines d'éléments.
    """
    if not points:
        return 0

    with_id = [(server_url, external_id, float(lat), float(lon)) for external_id, lat, lon in points if external_id is not None]
    without_id = [(server_url, None, float(lat), float(lon)) for external_id, lat, lon in points if external_id is None]

    with get_cursor(commit=True) as cur:
        if with_id:
            try:
                cur.executemany(
                    """
                    INSERT INTO gps_points (server_url, external_id, lat, lon)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(server_url, external_id) WHERE external_id IS NOT NULL DO UPDATE SET
                        lat = excluded.lat,
                        lon = excluded.lon
                    """,
                    with_id,
                )
            except sqlite3.OperationalError:
                # Repli : l'index unique requis par ON CONFLICT n'existe
                # pas (voir configure()) — insertion simple, created_at
                # ne sera alors pas préservé lors des resynchronisations
                # (le filtre `since` de /api/v1/nearby restera dégradé
                # jusqu'à ce que l'index puisse être créé).
                cur.executemany(
                    "INSERT INTO gps_points (server_url, external_id, lat, lon) VALUES (?, ?, ?, ?)",
                    with_id,
                )
        if without_id:
            cur.executemany(
                "INSERT INTO gps_points (server_url, external_id, lat, lon) VALUES (?, ?, ?, ?)",
                without_id,
            )
        return len(with_id) + len(without_id)


def delete_stale_points_for_server(server_url: str, keep_external_ids):
    """
    Supprime les points d'un serveur dont l'external_id n'est PAS dans
    `keep_external_ids` — cartes disparues du serveur distant depuis la
    dernière synchronisation complète (fusion de doublons, suppression,
    ...). Les points sans external_id (ancien format) ne sont jamais
    supprimés par cette fonction, faute de clé fiable pour les identifier
    individuellement.

    IMPORTANT : à n'appeler qu'après une synchronisation COMPLÈTE de
    /api/v1/gps (toutes les pages parcourues jusqu'au bout). L'appeler
    après une synchronisation partielle (erreur réseau en cours de
    pagination, garde-fou de pagination atteint, ...) supprimerait à tort
    des points simplement pas encore revus dans cette page — voir
    Poller._sync_server qui ne l'appelle que si `_download_all_points` a
    signalé une synchronisation complète.
    """
    keep_ids = {str(i) for i in keep_external_ids if i is not None}

    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT external_id FROM gps_points "
            "WHERE server_url = ? AND external_id IS NOT NULL",
            (server_url,),
        )
        existing_ids = {row["external_id"] for row in cur.fetchall()}

    stale_ids = list(existing_ids - keep_ids)
    if not stale_ids:
        return 0

    # Chunké pour rester sous la limite de paramètres SQLite (~999), même
    # avec des collections de plusieurs milliers de cartes. Contrairement
    # à un DELETE ... NOT IN (chunk) qu'il serait incorrect de chunker
    # (chaque chunk NOT IN ne "voit" pas les ids des autres chunks), un
    # DELETE ... IN (chunk) est sûr à chunker : chaque appel ne supprime
    # que les ids explicitement listés dans ce chunk.
    deleted = 0
    chunk_size = 500
    with get_cursor(commit=True) as cur:
        for i in range(0, len(stale_ids), chunk_size):
            chunk = stale_ids[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"DELETE FROM gps_points WHERE server_url = ? AND external_id IN ({placeholders})",
                [server_url, *chunk],
            )
            deleted += cur.rowcount
    return deleted


def delete_points_for_server(server_url: str):
    """
    Supprime les points GPS d'un serveur (sans toucher à sa ligne d'état),
    avant une resynchronisation complète — évite d'accumuler des doublons
    à chaque changement de dbid.
    """
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM gps_points WHERE server_url = ?", (server_url,))


def count_points():
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM gps_points")
        return cur.fetchone()["c"]


def update_server_capabilities(server_url: str, capabilities_json: str):
    """
    Enregistre en cache le JSON brut renvoyé par GET /api/v1/capabilities
    d'un serveur, récupéré par le poller à chaque cycle (indépendamment
    des changements de dbid : contrairement aux bounds, les capabilities
    peuvent changer sans qu'aucune carte ne soit ajoutée/modifiée — ex.
    activation de similar_search, création d'un premier compte manager,
    changement de min_supported_client).

    Stocké tel quel (chaîne JSON), reparsé à la lecture par
    /api/v1/servers (kartotek_master.app) — évite de dupliquer ici la
    connaissance du schéma de /api/v1/capabilities, qui appartient à
    flpostcards.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO servers_state (server_url, capabilities_json, capabilities_updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(server_url) DO UPDATE SET
                capabilities_json = excluded.capabilities_json,
                capabilities_updated_at = excluded.capabilities_updated_at
            """,
            (server_url, capabilities_json, now),
        )


def delete_server_data(server_url: str):
    """
    Supprime toutes les données d'un serveur (points GPS + ligne d'état),
    typiquement lorsqu'il a disparu de servers.json.
    """
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM gps_points WHERE server_url = ?", (server_url,))
        cur.execute("DELETE FROM servers_state WHERE server_url = ?", (server_url,))


def list_servers_state():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM servers_state ORDER BY server_url")
        return [dict(r) for r in cur.fetchall()]


def query_points_raw(min_lon, min_lat, max_lon, max_lat, limit):
    """Renvoie les points bruts contenus dans la bbox (utilisé en zoom élevé)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, external_id, lat, lon, server_url FROM gps_points
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            LIMIT ?
            """,
            (min_lat, max_lat, min_lon, max_lon, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def list_known_server_urls():
    """Ensemble des server_url connus (pour valider le paramètre `servers`
    reçu par les endpoints /api/v1/bounds, /nearby et /next-update — on
    n'interroge/n'expose jamais un serveur qui n'a pas été configuré dans
    servers.json)."""
    with get_cursor() as cur:
        cur.execute("SELECT server_url FROM servers_state")
        return {r["server_url"] for r in cur.fetchall()}


def query_points_for_servers(servers, min_lat=None, max_lat=None, min_lon=None, max_lon=None, limit=None, since=None):
    """
    Points GPS (id, external_id, lat, lon, server_url) déjà synchronisés
    en base par le poller, filtrés par une liste de server_url (aucun
    filtre si `servers` est vide/None : tous les serveurs connus),
    optionnellement par une bounding box (pré-filtre bon marché avant un
    calcul de distance haversine précis fait en Python par l'appelant),
    et optionnellement par `since` (timestamp UNIX) pour ne renvoyer que
    les points apparus dans le cache du master à partir de cette date
    (voir upsert_points : created_at n'est mis à jour QUE lors de la
    première apparition d'un point, pas à chaque resynchronisation).

    Sert de base au filtre `since` de /api/v1/nearby (geoapi.nearby),
    pensé pour le geofencing en arrière-plan de l'appli mobile : sans
    lui, chaque réveil du geofencing devrait retélécharger l'intégralité
    des cartes du rayon de recherche plutôt que seulement les nouvelles.

    Ne fait jamais d'appel réseau : c'est la base locale (déjà tenue à
    jour par kartotek_master.poller) qui sert de cache pour
    /api/v1/bounds, /api/v1/nearby et /api/v1/next-update, plutôt que de
    réinterroger chaque serveur distant à chaque requête client.
    """
    clauses = []
    params: list = []
    if servers:
        placeholders = ",".join("?" * len(servers))
        clauses.append(f"server_url IN ({placeholders})")
        params.extend(servers)
    if min_lat is not None:
        clauses.append("lat BETWEEN ? AND ?")
        params.extend([min_lat, max_lat])
    if min_lon is not None:
        clauses.append("lon BETWEEN ? AND ?")
        params.extend([min_lon, max_lon])
    if since is not None:
        clauses.append("created_at >= datetime(?, 'unixepoch')")
        params.append(int(since))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT id, external_id, lat, lon, server_url FROM gps_points {where}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def upsert_push_registration(token: str, platform: str, lat: float, lon: float, radius_m: float):
    """
    Enregistre ou remplace l'inscription push d'un appareil. Une
    ré-inscription avec le même token écrase entièrement l'entrée
    précédente (position/rayon peuvent avoir changé depuis).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO push_registrations (token, platform, lat, lon, radius_m, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                platform = excluded.platform,
                lat = excluded.lat,
                lon = excluded.lon,
                radius_m = excluded.radius_m,
                updated_at = excluded.updated_at
            """,
            (token, platform, lat, lon, radius_m, now),
        )


def delete_push_registration(token: str) -> bool:
    """Retourne True si une ligne existait et a été supprimée."""
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM push_registrations WHERE token = ?", (token,))
        return cur.rowcount > 0


def delete_push_registrations(tokens):
    """Suppression en lot (tokens signalés invalides par FCM/APNs)."""
    tokens = list(tokens)
    if not tokens:
        return
    with get_cursor(commit=True) as cur:
        cur.executemany("DELETE FROM push_registrations WHERE token = ?", [(t,) for t in tokens])


def purge_stale_push_registrations(max_age_days: float) -> int:
    """
    Supprime les inscriptions push dont `updated_at` n'a pas été renouvelé
    depuis plus de `max_age_days` jours.

    Complète (sans le remplacer) le nettoyage réactif fait par
    delete_push_registrations() sur retour 404/410 de FCM/APNs :
    celui-ci ne détecte un token mort que lorsqu'on tente RÉELLEMENT de
    lui envoyer une notification (donc jamais pour un appareil hors de
    toute zone surveillée, ou si aucune carte n'est ajoutée entre-temps),
    et peut mettre longtemps à réagir à une désinstallation sans
    /api/v1/push/unregister.

    Un appareil dont l'app tourne normalement renouvelle son inscription
    à chaque `POST /api/v1/push/register` (voir upsert_push_registration,
    appelé par l'app mobile a minima à chaque changement de position
    surveillée) : `updated_at` ne devient ancien que si l'appareil ne
    s'est plus jamais réinscrit, ce qui est le signal recherché ici.

    Retourne le nombre de lignes supprimées.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM push_registrations WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


def query_push_registrations_near(lat: float, lon: float, max_radius_m: float):
    """
    Pré-filtre bon marché en SQL (bounding box ~ max_radius_m autour de
    (lat, lon), converti grossièrement en degrés) avant un calcul
    haversine précis fait en Python par l'appelant (voir
    kartotek_master.push._registrations_in_range) — même logique que
    query_points_for_servers pour la carte.
    """
    # 1 degré de latitude ~= 111 km partout : marge simple et suffisante
    # pour un pré-filtre (pas besoin d'exactitude ici, juste de réduire
    # le nombre de lignes avant le calcul précis).
    deg_margin = (max_radius_m / 1000.0) / 111.0
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT token, platform, lat, lon, radius_m FROM push_registrations
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            """,
            (lat - deg_margin, lat + deg_margin, lon - deg_margin, lon + deg_margin),
        )
        return [dict(r) for r in cur.fetchall()]


def count_push_registrations() -> int:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM push_registrations")
        return cur.fetchone()["c"]


# ---------------------------------------------------------------------------
# Entitlements "version pro" (achats in-app validés côté serveur)
# ---------------------------------------------------------------------------

def upsert_purchase_entitlement(
    device_id: str, product_id: str, platform: str, status: str, purchase_ref: str,
) -> None:
    """
    Enregistre ou remplace l'entitlement d'un appareil pour un produit.
    Une revérification (même appareil, même produit) écrase l'entrée
    précédente — c'est voulu : c'est ainsi qu'un statut "revoked" (voir
    kartotek_master.purchases, remboursement/annulation détecté à la
    revérification) remplace un ancien statut "active".
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO purchase_entitlements
                (device_id, product_id, platform, status, purchase_ref, verified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, product_id) DO UPDATE SET
                platform = excluded.platform,
                status = excluded.status,
                purchase_ref = excluded.purchase_ref,
                verified_at = excluded.verified_at
            """,
            (device_id, product_id, platform, status, purchase_ref, now),
        )


def get_purchase_entitlement(device_id: str, product_id: str):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM purchase_entitlements WHERE device_id = ? AND product_id = ?",
            (device_id, product_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_purchase_entitlements(device_id: str) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM purchase_entitlements WHERE device_id = ?",
            (device_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def query_points_clustered(min_lon, min_lat, max_lon, max_lat, precision, limit):
    """
    Regroupe les points par cellule de grille (arrondi lat/lon) ET par
    serveur d'origine, puis renvoie le centre + le nombre de points par
    groupe. Le regroupement par serveur permet de garder une couleur par
    serveur même lorsque la carte est dézoomée et affiche des clusters.
    `precision` = nombre de décimales conservées (plus petit => cellules plus grandes).
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT
                server_url,
                ROUND(lat, ?) AS cell_lat,
                ROUND(lon, ?) AS cell_lon,
                COUNT(*) AS count,
                AVG(lat) AS lat,
                AVG(lon) AS lon
            FROM gps_points
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            GROUP BY server_url, cell_lat, cell_lon
            LIMIT ?
            """,
            (precision, precision, min_lat, max_lat, min_lon, max_lon, limit),
        )
        return [dict(r) for r in cur.fetchall()]
