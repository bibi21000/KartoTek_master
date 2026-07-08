"""
Poller en arrière-plan.

- Au démarrage : vérifie immédiatement (après un court délai) tous les
  serveurs de servers.json.
- Ensuite : se réveille toutes les `interval_minutes` pour revérifier.
- Pour chaque serveur : GET /api/v1/dbid. Si la valeur diffère de celle
  connue en base, on retélécharge l'intégralité des cartes via
  /api/v1/gps?start=&offset= en paginant :

      {
        "count": 42,    # résultats dans cette page
        "total": 150,   # cartes uniques totales (avec ou sans coordonnées)
        "cards": [{"id": "1", "lat": 46.749, "lon": 5.620}, ...]
      }

  Seules les cartes avec des coordonnées valides sont conservées. Les
  anciens points du serveur sont purgés avant réinsertion, pour éviter
  toute accumulation de doublons au fil des resynchronisations.
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
            [{"name": "serveur1", "url": "http://..."}, ...]

        Rétrocompatibilité : une simple liste de chaînes ["http://...", ...]
        est aussi acceptée (le nom affiché sera alors l'URL elle-même).

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
                else:
                    url = str(entry).rstrip("/")
                    name = url
                if url:
                    servers.append({"name": name, "url": url})
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

    # ------------------------------------------------------------- workflow
    def _sync_server(self, base_url, name):
        dbid_url = f"{base_url}/api/v1/dbid"
        try:
            resp = self._request_with_retry(dbid_url)
        except requests.RequestException as exc:
            logger.error("Impossible de joindre %s : %s", dbid_url, exc)
            db.touch_server_check(base_url, name=name, error=str(exc))
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
            db.touch_server_check(base_url, name=name)
            return

        logger.info(
            "Changement détecté pour %s (%s -> %s), synchronisation des points GPS...",
            base_url, current_dbid, new_dbid,
        )
        # On repart d'une base propre à chaque resynchronisation complète :
        # sans ça, les mêmes cartes seraient réinsérées à l'identique à
        # chaque changement de dbid, dupliquant les données au fil du temps.
        db.delete_points_for_server(base_url)
        total_inserted = self._download_all_points(base_url)
        db.update_server_dbid(base_url, new_dbid, total_inserted, name=name)
        logger.info("%s : %s points synchronisés", base_url, total_inserted)

    def _download_all_points(self, base_url):
        gps_url = f"{base_url}/api/v1/gps"
        start = 0
        total_inserted = 0
        while not self._stop_event.is_set():
            try:
                resp = self._request_with_retry(
                    gps_url, params={"start": start, "offset": self.page_size}
                )
            except requests.RequestException as exc:
                logger.error("Échec du téléchargement GPS depuis %s (start=%s) : %s", base_url, start, exc)
                break

            try:
                payload = resp.json()
            except ValueError:
                logger.error("Réponse GPS non-JSON depuis %s", gps_url)
                break

            cards, total = self._extract_cards(payload)
            if not cards:
                break

            # Seules les cartes avec des coordonnées valides sont géolocalisables.
            points = [
                (card.get("id"), card["lat"], card["lon"])
                for card in cards
                if card.get("lat") is not None and card.get("lon") is not None
            ]
            if points:
                db.insert_points(base_url, points)
                total_inserted += len(points)

            start += len(cards)

            # On s'arrête dès qu'on a couvert le total annoncé par le
            # serveur, ou (à défaut de `total` fiable) dès qu'une page
            # renvoie moins de résultats que demandé.
            if (total is not None and start >= total) or len(cards) < self.page_size:
                break
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
                self._sync_server(server["url"], server["name"])
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
