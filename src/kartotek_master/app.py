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
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, session
from flask_babel import Babel, get_locale

from . import db
from .colors import color_for_server
from .contact import bp as contact_bp
from .info import bp as info_bp
from .poller import Poller
from .seo import bp as seo_bp
from .status import bp as status_bp

# Langues prises en charge par l'application (sélection auto via l'en-tête
# Accept-Language du navigateur, surchargeable avec ?lang=xx).
SUPPORTED_LOCALES = ["fr", "en"]
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

    app.register_blueprint(info_bp)
    app.register_blueprint(contact_bp)
    app.register_blueprint(status_bp)
    # Sans préfixe : les fichiers de vérification de propriété (Google
    # Search Console, Bing...) doivent être servis exactement à la racine
    # du domaine, tout comme /sitemap.xml et /robots.txt.
    app.register_blueprint(seo_bp)

    map_cfg = config["map"]
    cluster_zoom_threshold = map_cfg.getint("cluster_zoom_threshold", fallback=14)
    max_points = map_cfg.getint("max_points_returned", fallback=5000)

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

    @app.route("/api/points")
    def api_points():
        """
        Renvoie les points GPS visibles dans la bbox demandée.
        - zoom >= cluster_zoom_threshold : points bruts (limités).
        - sinon : clusters agrégés en SQL (évite de transférer des dizaines
          de milliers de marqueurs au navigateur).

        Paramètres attendus : bbox=minLon,minLat,maxLon,maxLat & zoom=<int>
        """
        bbox = request.args.get("bbox", "")
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except (ValueError, AttributeError):
            return jsonify({"error": "paramètre bbox invalide, attendu: minLon,minLat,maxLon,maxLat"}), 400

        try:
            zoom = int(request.args.get("zoom", 6))
        except ValueError:
            zoom = 6

        limit = min(max_points, int(request.args.get("limit", max_points)))

        if zoom >= cluster_zoom_threshold:
            points = db.query_points_raw(min_lon, min_lat, max_lon, max_lat, limit)
            for p in points:
                p["color"] = color_for_server(p["server_url"])
            return jsonify({"type": "points", "zoom": zoom, "data": points})

        # Plus le zoom est faible, plus les cellules de regroupement sont grandes
        precision = max(1, min(5, zoom // 2))
        clusters = db.query_points_clustered(min_lon, min_lat, max_lon, max_lat, precision, limit)
        for c in clusters:
            c["color"] = color_for_server(c["server_url"])
        return jsonify({"type": "clusters", "zoom": zoom, "data": clusters})

    @app.route("/api/status")
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
