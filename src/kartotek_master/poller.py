"""
Poller en arrière-plan.

- Au démarrage : vérifie immédiatement (après un court délai) tous les
  serveurs de servers.json.
- Ensuite : se réveille toutes les `interval_minutes` pour revérifier.
- Pour chaque serveur : GET /api/v1/dbid. Si la valeur diffère de celle
  connue en base, on retélécharge l'intégralité des cartes via
  /api/v1/gps en paginant :

      {
        "count": 42,          # résultats dans cette page
        "total": 150,         # cartes uniques totales (avec ou sans coordonnées)
        "cards": [{"id": "1", "lat": 46.749, "lon": 5.620}, ...],
        "next_after_id": 42   # curseur pour la page suivante, null si terminé
      }

  Deux modes de pagination sont supportés :

  - **Par curseur** (préféré) : GET ?after_id=<curseur>&offset=<page_size>,
    en commençant par after_id=0, jusqu'à ce que `next_after_id` soit null.
    Stable même si la base bouge pendant le poll (contrairement à
    OFFSET/LIMIT, qui peut dupliquer ou sauter des lignes si le tri n'est
    pas parfaitement déterministe).
  - **Ancien mode** GET ?start=<n>&offset=<page_size> : toujours supporté
    en repli automatique pour les serveurs qui ne renvoient pas encore
    `next_after_id` dans leur réponse — détecté à la première page de
    chaque synchronisation. À migrer côté serveur dès que possible : ce
    mode reste théoriquement exposé au même risque de doublons/omissions
    si la base évolue en cours de pagination (voir les logs de synthèse
    en fin de synchronisation, qui signalent ces deux cas).

  Seules les cartes avec des coordonnées valides sont conservées. Les
  anciens points du serveur sont purgés avant réinsertion, pour éviter
  toute accumulation de doublons au fil des resynchronisations.
- Quand le dbid change (en même temps que la resynchronisation des cartes) :
  GET /api/v1/bounds pour connaître l'étendue géographique de la
  collection de chaque serveur, utilisée pour trier /api/v1/servers par
  proximité :

      {"count": 42, "bounds": {"min_lat":.., "max_lat":.., "min_lon":.., "max_lon":..}}

  `count` n'est volontairement pas exploité.
- Toute erreur réseau sur un serveur est loguée et n'empêche pas de
  traiter les autres serveurs, ni les prochains cycles (robustesse).
"""

import json
import logging
import threading
import time
from pathlib import Path

import requests

from . import db

logger = logging.getLogger("kartotek-master.poller")


class Poller:
    def __init__(self, config, base_dir=None):
        self.config = config
        base_dir = Path(base_dir) if base_dir else Path(".")
        servers_file = config.get("polling", "servers_file", fallback="conf/servers.json")
        self.servers_file = (base_dir / servers_file).resolve()
        self.interval_seconds = config.getfloat("polling", "interval_minutes", fallback=15) * 60
        self.initial_delay = config.getfloat("polling", "initial_delay_seconds", fallback=3)
        self.page_size = config.getint("api", "gps_page_size", fallback=500)
        self.timeout = config.getfloat("api", "request_timeout_seconds", fallback=10)
        self.retry_attempts = config.getint("api", "retry_attempts", fallback=3)
        self.retry_backoff = config.getfloat("api", "retry_backoff_seconds", fallback=2)
        self._stop_event = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- utils
    def _load_servers(self):
        """
        servers.json attendu :
            [{"name": "serveur1", "url": "http://...", "description": "..."}, ...]

        `description` est optionnelle (chaîne vide si absente).

        Rétrocompatibilité : une simple liste de chaînes ["http://...", ...]
        est aussi acceptée (le nom affiché sera alors l'URL elle-même, sans
        description).

        Renvoie `None` en cas d'erreur de lecture/format (fichier absent ou
        invalide) — à distinguer d'une liste valide mais vide. Ça évite de
        purger toutes les données existantes à cause d'un fichier temporairement
        illisible.
        """
        try:
            with open(self.servers_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("servers.json doit contenir une liste")

            servers = []
            for entry in raw:
                if isinstance(entry, dict):
                    url = str(entry.get("url", "")).rstrip("/")
                    name = entry.get("name") or url
                    description = str(entry.get("description") or "").strip()
                else:
                    url = str(entry).rstrip("/")
                    name = url
                    description = ""
                if url:
                    servers.append({"name": name, "url": url, "description": description})
            return servers
        except FileNotFoundError:
            logger.error("Fichier %s introuvable, aucun serveur à interroger", self.servers_file)
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("servers.json invalide : %s", exc)
            return None

    def _request_with_retry(self, url, params=None):
        last_exc = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Tentative %s/%s échouée pour %s : %s",
                    attempt, self.retry_attempts, url, exc,
                )
                if attempt < self.retry_attempts:
                    time.sleep(self.retry_backoff)
        raise last_exc

    @staticmethod
    def _extract_dbid(payload):
        """Le endpoint peut renvoyer un JSON {"dbid": ...} ou une valeur brute."""
        if isinstance(payload, dict):
            return str(payload.get("dbid", payload))
        return str(payload)

    @staticmethod
    def _extract_cards(payload):
        """
        Nouveau format attendu :
            {"count": 42, "total": 150, "cards": [{"id": "1", "lat": ..., "lon": ...}, ...]}

        `total` est le nombre total de cartes connues côté serveur, avec ou
        sans coordonnées — il sert à savoir quand arrêter la pagination.

        Rétrocompatibilité : l'ancien format [[lat, lon], ...] est aussi
        accepté (les cartes obtenues n'auront alors pas d'identifiant).

        Renvoie (cards, total) où `cards` est une liste de dicts
        {"id":..., "lat":..., "lon":...} et `total` un entier ou None si
        inconnu/non fourni par le serveur.
        """
        if isinstance(payload, dict) and "cards" in payload:
            cards = payload.get("cards") or []
            total = payload.get("total")
            return cards, total

        # Rétrocompatibilité avec l'ancien format (simple liste de paires)
        if isinstance(payload, list):
            cards = [
                {"id": None, "lat": item[0], "lon": item[1]}
                for item in payload
                if isinstance(item, (list, tuple)) and len(item) == 2
            ]
            return cards, None

        return [], None

    def _fetch_bounds(self, base_url):
        """
        Récupère l'étendue géographique (bounding box) de la collection
        d'un serveur, via GET /api/v1/bounds :
            {"count": ..., "bounds": {"min_lat":.., "max_lat":.., "min_lon":.., "max_lon":..}}
        Appelée uniquement quand le dbid d'un serveur change (en même temps
        que la resynchronisation des cartes) — pas à chaque cycle, les
        bounds n'étant censées bouger qu'avec le contenu de la collection.
        `count` n'est volontairement pas utilisé. Une erreur ici est
        loguée mais n'interrompt jamais la synchronisation des cartes GPS
        du serveur.
        """
        bounds_url = f"{base_url}/api/v1/bounds"
        try:
            resp = self._request_with_retry(bounds_url)
        except requests.RequestException as exc:
            logger.warning("Impossible de récupérer les bounds de %s : %s", base_url, exc)
            return

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Réponse bounds non-JSON depuis %s", bounds_url)
            return

        bounds = payload.get("bounds") if isinstance(payload, dict) else None
        if not isinstance(bounds, dict):
            logger.warning("Réponse bounds invalide depuis %s : %r", bounds_url, payload)
            return

        try:
            min_lat = float(bounds["min_lat"])
            max_lat = float(bounds["max_lat"])
            min_lon = float(bounds["min_lon"])
            max_lon = float(bounds["max_lon"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Champs bounds manquants/invalides depuis %s : %s", bounds_url, exc)
            return

        db.update_server_bounds(base_url, min_lat, max_lat, min_lon, max_lon)

    # ------------------------------------------------------------- workflow
    def _sync_server(self, base_url, name, description=""):
        dbid_url = f"{base_url}/api/v1/dbid"
        try:
            resp = self._request_with_retry(dbid_url)
        except requests.RequestException as exc:
            logger.error("Impossible de joindre %s : %s", dbid_url, exc)
            db.touch_server_check(base_url, name=name, description=description, error=str(exc))
            return

        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        new_dbid = self._extract_dbid(payload)

        state = db.get_server_state(base_url)
        current_dbid = state["dbid"] if state else None

        if current_dbid is not None and current_dbid == new_dbid:
            logger.debug("Aucun changement pour %s (dbid=%s)", base_url, new_dbid)
            db.touch_server_check(base_url, name=name, description=description)
            return

        # Uniquement quand le dbid change : les bounds sont censées ne
        # bouger qu'avec le contenu de la collection, pas besoin de les
        # réinterroger à chaque cycle si rien n'a changé côté serveur.
        self._fetch_bounds(base_url)

        logger.info(
            "Changement détecté pour %s (%s -> %s), synchronisation des points GPS...",
            base_url, current_dbid, new_dbid,
        )
        # On repart d'une base propre à chaque resynchronisation complète :
        # sans ça, les mêmes cartes seraient réinsérées à l'identique à
        # chaque changement de dbid, dupliquant les données au fil du temps.
        db.delete_points_for_server(base_url)
        total_inserted = self._download_all_points(base_url)
        db.update_server_dbid(base_url, new_dbid, total_inserted, name=name, description=description)
        logger.info("%s : %s points synchronisés", base_url, total_inserted)

    def _download_all_points(self, base_url):
        gps_url = f"{base_url}/api/v1/gps"
        cursor_after_id = 0
        legacy_start = 0
        use_cursor = None  # déterminé à la 1ere page, selon la présence de next_after_id
        total_inserted = 0
        pages_fetched = 0
        seen_ids = set()
        duplicates_skipped = 0
        cards_without_coords = 0
        last_known_total = None
        # Garde-fou : si `total`/`next_after_id` était mal renseigné par le
        # serveur distant (jamais atteint alors que les pages continuent
        # d'arriver), on évite une pagination infinie. Large marge au-delà
        # de ce qu'on peut raisonnablement attendre, juste pour couper un
        # cas pathologique.
        max_pages = 20000
        while not self._stop_event.is_set():
            pages_fetched += 1
            if pages_fetched > max_pages:
                logger.error(
                    "%s : garde-fou de pagination atteint (%s pages), arrêt "
                    "(vérifiez le champ 'total'/'next_after_id' de /api/v1/gps)",
                    base_url, max_pages,
                )
                break

            if use_cursor is False:
                # Mode legacy déjà confirmé pour ce serveur (pas de
                # next_after_id dans sa réponse) : on reste sur start/offset
                # pour toute la synchronisation.
                params = {"start": legacy_start, "offset": self.page_size}
            else:
                # Mode par défaut (pas encore déterminé, ou curseur déjà
                # confirmé) : on envoie after_id. Un serveur pas encore
                # migré ignore simplement ce paramètre inconnu et retombe
                # sur son comportement start=0 par défaut — sans risque.
                params = {"after_id": cursor_after_id, "offset": self.page_size}

            try:
                resp = self._request_with_retry(gps_url, params=params)
            except requests.RequestException as exc:
                logger.error("Échec du téléchargement GPS depuis %s (%s) : %s", base_url, params, exc)
                break

            try:
                payload = resp.json()
            except ValueError:
                logger.error("Réponse GPS non-JSON depuis %s", gps_url)
                break

            if use_cursor is None:
                use_cursor = isinstance(payload, dict) and "next_after_id" in payload
                if use_cursor:
                    logger.debug("%s : pagination par curseur (after_id) détectée", base_url)
                else:
                    logger.debug(
                        "%s : 'next_after_id' absent de la réponse, repli sur "
                        "l'ancien mode start/offset (à migrer côté serveur)",
                        base_url,
                    )

            cards, total = self._extract_cards(payload)
            if not cards:
                break
            if total is not None:
                last_known_total = total

            # On déduplique par identifiant *avant* de filtrer sur les
            # coordonnées : sinon une carte sans lat/lon qui reviendrait sur
            # plusieurs pages ne serait jamais comptée comme doublon. Seules
            # les cartes uniques avec coordonnées valides sont ensuite
            # géolocalisables ; celles sans coordonnées sont légitimement
            # ignorées (mais comptabilisées, pour distinguer ce cas d'une
            # vraie perte de données).
            points = []
            for card in cards:
                external_id = card.get("id")
                if external_id is not None:
                    if external_id in seen_ids:
                        duplicates_skipped += 1
                        continue
                    seen_ids.add(external_id)
                lat, lon = card.get("lat"), card.get("lon")
                if lat is None or lon is None:
                    cards_without_coords += 1
                    continue
                points.append((external_id, lat, lon))

            if points:
                db.insert_points(base_url, points)
                total_inserted += len(points)

            if use_cursor:
                next_after_id = payload.get("next_after_id")
                if next_after_id is None:
                    break
                cursor_after_id = next_after_id
            else:
                legacy_start += len(cards)
                if total is not None:
                    # `total` fait foi : certains serveurs renvoient des
                    # pages plus courtes que `offset` sans que ce soit la
                    # dernière page (ex. count=466 alors que offset=500 et
                    # total=601). S'arrêter dès qu'une page est "courte"
                    # ferait perdre les entrées restantes ; on ne s'arrête
                    # donc que lorsque le total annoncé est réellement atteint.
                    if len(cards) < self.page_size:
                        logger.debug(
                            "%s : page à start=%s a renvoyé %s élément(s) (< offset=%s) "
                            "mais total=%s non atteint (%s), on continue la pagination",
                            base_url, legacy_start - len(cards), len(cards),
                            self.page_size, total, legacy_start,
                        )
                    if legacy_start >= total:
                        break
                elif len(cards) < self.page_size:
                    # Pas de `total` fiable (ancien format d'API) : on
                    # s'arrête dès qu'une page renvoie moins d'éléments que
                    # demandé.
                    break

        mode_label = "curseur (after_id)" if use_cursor else "legacy (start/offset, à migrer côté serveur)"
        if duplicates_skipped:
            logger.warning(
                "%s [mode %s] : %s carte(s) reçue(s) en double pendant la "
                "pagination (même id renvoyé sur plusieurs pages), ignorées. "
                "%s",
                base_url, mode_label, duplicates_skipped,
                "Inattendu en mode curseur : la base a peut-être bougé "
                "pendant le poll, ou le serveur a un bug de génération de "
                "next_after_id — à investiguer côté serveur." if use_cursor
                else "Cause habituelle : tri SQL non déterministe combiné à "
                "une pagination OFFSET/LIMIT, pas un bug du poller.",
            )
        if cards_without_coords:
            logger.info(
                "%s : %s carte(s) unique(s) sans coordonnées (lat/lon absents), "
                "non géolocalisées — comportement normal, pas une erreur.",
                base_url, cards_without_coords,
            )
        unique_cards_seen = total_inserted + cards_without_coords
        if last_known_total is not None and unique_cards_seen != last_known_total:
            # Après déduplication, s'il manque encore des cartes par rapport
            # au total annoncé, ce ne sont ni des doublons ni des cartes
            # sans coordonnées : elles n'ont simplement jamais été
            # renvoyées, sur aucune page.
            logger.warning(
                "%s [mode %s] : %s carte(s) unique(s) vue(s) au total (dont "
                "%s sans coordonnées) contre un total annoncé de %s — %s "
                "carte(s) jamais renvoyée(s) sur aucune page. %s",
                base_url, mode_label, unique_cards_seen, cards_without_coords,
                last_known_total, last_known_total - unique_cards_seen,
                "En mode curseur, ceci ne devrait normalement plus se "
                "produire : signale soit un bug côté serveur (next_after_id "
                "mal calculé), soit un décompte 'total' incorrect — à "
                "investiguer côté serveur." if use_cursor
                else "Probablement le même souci de tri instable côté "
                "serveur distant, mais cette fois des cartes 'sautent' hors "
                "de la pagination au lieu d'y être dupliquées.",
            )
        return total_inserted

    def _prune_removed_servers(self, servers):
        """
        Supprime de la base (points GPS + état) tout serveur connu qui
        n'apparaît plus dans servers.json.
        """
        current_urls = {s["url"] for s in servers}
        known_urls = {row["server_url"] for row in db.list_servers_state()}
        removed = known_urls - current_urls
        for url in removed:
            logger.info(
                "%s n'est plus présent dans servers.json, suppression de ses données...", url
            )
            try:
                db.delete_server_data(url)
            except Exception:
                logger.exception("Échec de la suppression des données de %s", url)

    def _run_once(self):
        servers = self._load_servers()
        if servers is None:
            # Fichier absent/invalide : on ne touche à rien, on retentera au
            # prochain cycle.
            return

        self._prune_removed_servers(servers)

        for server in servers:
            if self._stop_event.is_set():
                return
            try:
                self._sync_server(server["url"], server["name"], server["description"])
            except Exception:
                # Filet de sécurité : un bug sur un serveur ne doit jamais tuer le thread
                logger.exception("Erreur inattendue lors du traitement de %s", server["url"])

    def _loop(self):
        if self._stop_event.wait(self.initial_delay):
            return
        while not self._stop_event.is_set():
            self._run_once()
            self._stop_event.wait(self.interval_seconds)

    # --------------------------------------------------------------- public
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="gps-poller", daemon=True)
        self._thread.start()
        logger.info(
            "Poller démarré (intervalle=%.0fs, délai initial=%.0fs)",
            self.interval_seconds, self.initial_delay,
        )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
