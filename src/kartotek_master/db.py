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

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
            # Index composites pour accélérer les filtres bbox (WHERE lat BETWEEN.. AND lon BETWEEN..)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_lat ON gps_points(lat);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_lon ON gps_points(lon);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_server ON gps_points(server_url);")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_gps_lat_lon_server ON gps_points(lat, lon, server_url);"
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


def insert_points(server_url: str, points):
    """
    points : itérable de (external_id, lat, lon). `external_id` (l'identifiant
    de la carte côté serveur distant) peut être `None` si non fourni.
    Insertion en une seule transaction pour limiter les I/O disque, même si
    la page contient des centaines d'éléments.
    """
    if not points:
        return 0
    rows = [
        (server_url, external_id, float(lat), float(lon))
        for external_id, lat, lon in points
    ]
    with get_cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO gps_points (server_url, external_id, lat, lon) VALUES (?, ?, ?, ?)",
            rows,
        )
        return cur.rowcount


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


def query_points_for_servers(servers, min_lat=None, max_lat=None, min_lon=None, max_lon=None, limit=None):
    """
    Points GPS (id, external_id, lat, lon, server_url) déjà synchronisés
    en base par le poller, filtrés par une liste de server_url (aucun
    filtre si `servers` est vide/None : tous les serveurs connus) et,
    optionnellement, par une bounding box (pré-filtre bon marché avant un
    calcul de distance haversine précis fait en Python par l'appelant).

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

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT id, external_id, lat, lon, server_url FROM gps_points {where}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    with get_cursor() as cur:
        cur.execute(sql, params)
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
