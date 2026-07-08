"""
Configuration gunicorn — application volontairement peu gourmande, donc
peu de workers par défaut.

Les paramètres sont lus depuis la section [gunicorn] de conf/config.conf
(chemin surchargeable avec la variable d'environnement
KARTOTEK_MASTER_CONF). Chaque valeur reste en plus surchargeable
individuellement par une variable d'environnement KARTOTEK_MASTER_<NOM>
(ex : KARTOTEK_MASTER_WORKERS=2), pratique pour adapter un déploiement
sans toucher au fichier de config.

Utilisation :
    gunicorn -c gunicorn.conf.py kartotek_master.wsgi:app
    KARTOTEK_MASTER_CONF=/etc/kartotek-master/config.conf gunicorn -c gunicorn.conf.py kartotek_master.wsgi:app
"""

import configparser
import multiprocessing
import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("KARTOTEK_MASTER_HOME", ".")).resolve()
CONFIG_PATH = Path(os.environ.get("KARTOTEK_MASTER_CONF", BASE_DIR / "conf" / "config.conf"))

_config = configparser.ConfigParser()
if CONFIG_PATH.exists():
    _config.read(CONFIG_PATH, encoding="utf-8")
_gunicorn_section = _config["gunicorn"] if _config.has_section("gunicorn") else {}


def _setting(name, default, cast=str):
    """Priorité : variable d'environnement > conf/config.conf > défaut codé en dur."""
    env_value = os.environ.get(f"KARTOTEK_MASTER_{name.upper()}")
    if env_value is not None:
        return cast(env_value)
    if name in _gunicorn_section:
        return cast(_gunicorn_section[name])
    return default


bind = _setting("bind", "0.0.0.0:5000")

# Par défaut, UN SEUL worker : le thread de polling démarre par process,
# donc plusieurs workers dupliqueraient les synchronisations. Les requêtes
# concurrentes sont gérées via plusieurs threads dans ce worker unique.
# N'augmentez le nombre de workers que si vous avez adapté la logique du
# poller (verrou externe, process dédié, etc.).
workers = _setting("workers", 1, int)
worker_class = "gthread"
threads = _setting("threads", max(4, multiprocessing.cpu_count() * 2), int)

timeout = _setting("timeout", 30, int)
graceful_timeout = _setting("graceful_timeout", 30, int)
keepalive = _setting("keepalive", 5, int)

accesslog = _setting("accesslog", "logs/gunicorn-access.log")
errorlog = _setting("errorlog", "logs/gunicorn-error.log")
loglevel = _setting("loglevel", "info")

preload_app = False
