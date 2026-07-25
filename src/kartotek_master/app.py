"""
Application Flask légère : carte GPS alimentée par une synchronisation
périodique de serveurs distants, données stockées en SQLite (sqlite3 pur,
sans ORM).

Démarrage (dev) :
    python -m kartotek_master.app
    # ou, une fois le package installé :
    kartotek-master

Démarrage (prod) :
    gunicorn -c gunicorn.conf.py kartotek_master.wsgi:app

Les fichiers `conf/config.conf` et `conf/servers.json`, ainsi que les
dossiers `data/` et `logs/`, sont cherchés à la racine du projet (le
répertoire courant au lancement), et non dans le package installé — cela
permet de faire évoluer la configuration sans reconstruire le paquet. Le
chemin de base peut être surchargé avec la variable d'environnement
KARTOTEK_MASTER_HOME, et le chemin exact du fichier de config avec
KARTOTEK_MASTER_CONF (également utilisée par gunicorn.conf.py).
"""

import configparser
import logging
import logging.handlers
import os
import signal
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from babel.dates import format_datetime as babel_format_datetime
from flask import Flask, jsonify, render_template, request, session
from flask_babel import Babel, get_locale

from . import db
from .colors import color_for_server
from .contact import bp as contact_bp
from .download import bp as download_bp
from .geo import haversine_km
from .geoapi import bp as geoapi_bp
from .info import bp as info_bp
from .limiter import limiter
from .poller import Poller
from .seo import bp as seo_bp
from .status import bp as status_bp
from .submit_server import bp as submit_server_bp

# Langues prises en charge par l'application (sélection auto via l'en-tête
# Accept-Language du navigateur, surchargeable avec ?lang=xx).
SUPPORTED_LOCALES = ["fr", "en", "uk"]
DEFAULT_LOCALE = "fr"

# Racine du projet : par défaut le répertoire courant (là où l'on lance
# `kartotek-master` / `gunicorn` / `make run`), surchargeable via l'environnement.
BASE_DIR = Path(os.environ.get("KARTOTEK_MASTER_HOME", ".")).resolve()


def load_config():
    config = configparser.ConfigParser()
    config_path = Path(os.environ.get("KARTOTEK_MASTER_CONF", BASE_DIR / "conf" / "config.conf"))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {config_path}. "
            "Lancez la commande depuis la racine du projet, ou définissez "
            "KARTOTEK_MASTER_HOME / KARTOTEK_MASTER_CONF."
        )
    config.read(config_path, encoding="utf-8")
    return config


def setup_logging(config):
    level_name = config.get("logging", "level", fallback="INFO")
    log_file = config.get("logging", "file", fallback=None)
    level = getattr(logging, level_name.upper(), logging.INFO)

    handlers = [logging.StreamHandler()]
    if log_file:
        log_path = BASE_DIR / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
        )

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def create_app():
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger("kartotek-master.app")

    db.configure(str(BASE_DIR / config.get("database", "path", fallback="data/gps_data.db")))
    db.init_db()

    app = Flask(__name__)
    app.config["APP_CONFIG"] = config
    # Répertoire de données applicatives exposées en HTTP (fichiers de
    # vérification de propriété pour l'indexation...), configurable via
    # html_dir dans [flask] (relatif à la racine du projet, ou chemin
    # absolu). Réutilise le dossier data/ existant par défaut.
    app.config["HTMLDIR"] = str(BASE_DIR / config.get("flask", "html_dir", fallback="data"))
    # Répertoire contenant les fichiers proposés au téléchargement sur la
    # page /download/ (paquets, installeurs...), configurable via
    # download_dir dans [flask] (relatif à la racine du projet, ou
    # chemin absolu).
    download_dir = Path(config.get("flask", "download_dir", fallback="data/downloads"))
    if not download_dir.is_absolute():
        download_dir = BASE_DIR / download_dir
    app.config["DOWNLOAD_DIR"] = str(download_dir)

    # Requis par flash()/session (utilisés par le blueprint contact) : sans
    # clé, Flask lève une erreur à la première tentative d'écriture en
    # session. À défaut d'une clé fournie dans config.conf, on en génère une
    # aléatoire pour que l'appli démarre quand même (les sessions ne
    # survivront alors pas à un redémarrage — acceptable en dev, mais
    # définissez `secret_key` dans [flask] pour un déploiement durable).
    secret_key = config.get("flask", "secret_key", fallback="")
    if not secret_key:
        secret_key = os.urandom(32).hex()
        logger.warning(
            "Aucune 'secret_key' dans [flask] de config.conf : une clé "
            "aléatoire a été générée pour cette exécution (les sessions "
            "ne persisteront pas après un redémarrage)."
        )
    app.config["SECRET_KEY"] = secret_key

    # Flask-Limiter : protège certaines routes (ex. /download) contre les
    # abus. Le backend de comptage est configurable via
    # `rate_limit_storage_uri` dans [flask] (ex. "memory://" en dev,
    # "redis://localhost:6379" en prod pour un comptage partagé entre
    # workers gunicorn). Sans valeur, on retombe sur un stockage en
    # mémoire du process (acceptable en dev, mais chaque worker aurait
    # alors son propre compteur en prod avec plusieurs workers).
    app.config["RATELIMIT_STORAGE_URI"] = config.get(
        "flask", "rate_limit_storage_uri", fallback="memory://"
    )
    # Préfixe des clés utilisées dans le backend de stockage, pour éviter
    # les collisions si ce dernier est partagé avec d'autres applications.
    app.config["RATELIMIT_KEY_PREFIX"] = config.get(
        "flask", "rate_limit_key_prefix", fallback="kartotek-master"
    )
    limiter.init_app(app)

    # Suivi des visites Matomo : hôte du serveur Matomo, configurable via
    # site_matomo dans [flask] (ex. "matomo.example.com"). Si absent ou
    # vide, le code de tracking n'est pas injecté dans les pages (voir
    # templates/base.html). id_matomo est l'identifiant du site dans
    # Matomo (setSiteId), configurable via id_matomo dans [flask].
    app.config["SITE_MATOMO"] = config.get("flask", "site_matomo", fallback="")
    app.config["ID_MATOMO"] = config.get("flask", "id_matomo", fallback="1")
    app.jinja_env.globals["site_matomo"] = app.config["SITE_MATOMO"]
    app.jinja_env.globals["id_matomo"] = app.config["ID_MATOMO"]

    # Flask-Babel : langue choisie via ?lang=xx (mémorisée en session), sinon
    # l'en-tête Accept-Language du navigateur, sinon le français par défaut.
    def _select_locale():
        forced = session.get("lang")
        if forced in SUPPORTED_LOCALES:
            return forced
        return request.accept_languages.best_match(SUPPORTED_LOCALES, default=DEFAULT_LOCALE)

    app.config.setdefault("BABEL_DEFAULT_LOCALE", DEFAULT_LOCALE)
    Babel(app, locale_selector=_select_locale)
    app.jinja_env.globals["get_locale"] = lambda: str(get_locale())

    @app.template_filter("localdatetime")
    def localdatetime_filter(value, fmt="medium"):
        """
        Formate une date/heure stockée en base (chaîne ISO 8601, ex.
        "2026-07-09T06:31:29.482123+00:00") selon la langue courante.
        Renvoie None si la valeur est vide/non parseable, à gérer côté
        template (ex. `{{ v|localdatetime or '—' }}`). Les dates sont
        stockées en UTC ; seul le format change avec la langue, pas le
        fuseau horaire (on ne connaît pas celui de l'utilisateur).
        """
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value
        return babel_format_datetime(dt, format=fmt, locale=str(get_locale()))

    @app.before_request
    def _apply_lang_override():
        lang = request.args.get("lang")
        if lang in SUPPORTED_LOCALES:
            session["lang"] = lang

    # Configuration du formulaire de contact (section [contact] de
    # config.conf). La page reste désactivée (404) tant que contact_email
    # et smtp_host ne sont pas renseignés — voir kartotek_master.contact.
    if config.has_section("contact"):
        contact_cfg = config["contact"]
        app.config["CONTACT_EMAIL"] = contact_cfg.get("contact_email") or None
        app.config["SMTP_HOST"] = contact_cfg.get("smtp_host") or None
        app.config["SMTP_PORT"] = contact_cfg.getint("smtp_port", fallback=587)
        app.config["SMTP_USER"] = contact_cfg.get("smtp_user", fallback="")
        app.config["SMTP_PASSWORD"] = contact_cfg.get("smtp_password", fallback="")
        app.config["SMTP_SECURITY"] = contact_cfg.get("smtp_security", fallback="starttls")

    map_cfg = config["map"]
    cluster_zoom_threshold = map_cfg.getint("cluster_zoom_threshold", fallback=14)
    max_points = map_cfg.getint("max_points_returned", fallback=5000)
    # Exposés via app.config pour que le blueprint geoapi (qui porte
    # désormais /api/v1/points, en plus de bounds/nearby/next-update)
    # puisse les lire avec current_app.config sans dépendre de la closure
    # de create_app().
    app.config["CLUSTER_ZOOM_THRESHOLD"] = cluster_zoom_threshold
    app.config["MAX_POINTS_RETURNED"] = max_points

    app.register_blueprint(info_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(contact_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(submit_server_bp)
    # /api/v1/points, /api/v1/bounds, /api/v1/nearby, /api/v1/next-update
    # — endpoints géo, servis depuis le cache local (voir kartotek_master.geoapi).
    app.register_blueprint(geoapi_bp)
    # Sans préfixe : les fichiers de vérification de propriété (Google
    # Search Console, Bing...) doivent être servis exactement à la racine
    # du domaine, tout comme /sitemap.xml et /robots.txt.
    app.register_blueprint(seo_bp)

    # --------------------------------------------------------------- routes
    @app.route("/")
    def index():
        return render_template(
            "index.html",
            default_lat=map_cfg.getfloat("default_lat", fallback=46.6),
            default_lon=map_cfg.getfloat("default_lon", fallback=1.9),
            default_zoom=map_cfg.getint("default_zoom", fallback=6),
            cluster_zoom_threshold=cluster_zoom_threshold,
        )


    def api_status():
        servers = db.list_servers_state()
        for s in servers:
            s["color"] = color_for_server(s["server_url"])
        return jsonify(
            {
                "total_points": db.count_points(),
                "servers": servers,
            }
        )

    @app.route("/api/search")
    def api_search():
        """
        Proxy vers Nominatim (OpenStreetMap) pour la recherche de lieux,
        afin d'éviter les soucis CORS côté navigateur et de maîtriser le
        User-Agent exigé par la politique d'usage de Nominatim.
        """
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 5},
                headers={"User-Agent": "kartotek-master-app/1.0"},
                timeout=5,
            )
            resp.raise_for_status()
            return jsonify(resp.json())
        except requests.RequestException as exc:
            logger.warning("Recherche Nominatim échouée : %s", exc)
            return jsonify({"error": "recherche indisponible"}), 502

    @app.route("/api/v1/servers")
    def api_v1_servers():
        """
        Liste des serveurs connus, triée par proximité avec la position GPS
        du client (query params `lat`/`lon`), en utilisant la bounding box
        de chaque serveur (récupérée périodiquement via /api/v1/bounds par
        le poller — voir kartotek_master.poller). La distance est calculée
        jusqu'au centre de cette bounding box.

        Les serveurs sans bounds connues (pas encore récupérées, ou serveur
        injoignable) sont inclus mais placés en fin de liste, sans
        `distance_km`. Sans lat/lon fournis, la liste est simplement triée
        par nom.
        """
        try:
            user_lat = float(request.args["lat"])
            user_lon = float(request.args["lon"])
            has_position = True
        except (KeyError, TypeError, ValueError):
            has_position = False
            user_lat = user_lon = None

        results = []
        for s in db.list_servers_state():
            entry = {
                "name": s.get("name") or s["server_url"],
                "url": s["server_url"],
                "description": s.get("description") or "",
                # Favicon servi par le serveur distant lui-même, à sa racine.
                # On ne vérifie pas son existence ici (coûteux et non bloquant
                # côté poller) : le client gère l'absence via l'événement
                # onerror de l'<img>.
                "favicon": urljoin(s["server_url"], "/favicon.ico"),
                "distance_km": None,
            }
            min_lat = s.get("bounds_min_lat")
            max_lat = s.get("bounds_max_lat")
            min_lon = s.get("bounds_min_lon")
            max_lon = s.get("bounds_max_lon")
            if None not in (min_lat, max_lat, min_lon, max_lon):
                entry["bounds"] = {
                    "min_lat": min_lat, "max_lat": max_lat,
                    "min_lon": min_lon, "max_lon": max_lon,
                }
                if has_position:
                    center_lat = (min_lat + max_lat) / 2
                    center_lon = (min_lon + max_lon) / 2
                    entry["distance_km"] = round(
                        haversine_km(user_lat, user_lon, center_lat, center_lon), 1
                    )
            results.append(entry)

        if has_position:
            # Les serveurs sans distance connue (pas de bounds) vont en fin de liste.
            results.sort(key=lambda e: e["distance_km"] if e["distance_km"] is not None else float("inf"))
        else:
            results.sort(key=lambda e: e["name"].lower())

        return jsonify({"servers": results})

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    # ------------------------------------------------------------- démarrage
    poller = Poller(config, base_dir=BASE_DIR)
    app.extensions = getattr(app, "extensions", {})
    app.extensions["poller"] = poller

    return app, config


app, _config = create_app()


def _start_background_poller():
    """
    Ne démarre le thread qu'une seule fois, même avec le reloader Flask
    (qui lance le module deux fois en mode debug).
    """
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        app.extensions["poller"].start()


def _handle_shutdown(signum, frame):
    logging.getLogger("kartotek-master.app").info("Arrêt demandé, on stoppe le poller proprement...")
    app.extensions["poller"].stop()
    raise SystemExit(0)


# Démarre le poller dès l'import du module : couvre à la fois le lancement
# direct (`python -m kartotek_master.app`) et le chargement par un serveur WSGI
# (gunicorn importe `kartotek_master.wsgi`, qui importe ce module).
_start_background_poller()


def main():
    """Point d'entrée du script console `kartotek-master` (serveur de développement)."""
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    flask_cfg = _config["flask"]
    app.run(
        host=flask_cfg.get("host", fallback="0.0.0.0"),
        port=flask_cfg.getint("port", fallback=5000),
        debug=flask_cfg.getboolean("debug", fallback=False),
    )


if __name__ == "__main__":
    main()
