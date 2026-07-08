#!/usr/bin/env bash
#
# Lance l'application en production avec gunicorn.
# À exécuter depuis la racine du projet (là où se trouvent conf/config.conf,
# conf/servers.json, gunicorn.conf.py).
#
# Usage :
#   ./scripts/run_gunicorn.sh
#   KARTOTEK_MASTER_BIND=0.0.0.0:8000 KARTOTEK_MASTER_WORKERS=1 ./scripts/run_gunicorn.sh
#   KARTOTEK_MASTER_CONF=/etc/kartotek-master/config.conf ./scripts/run_gunicorn.sh
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="${VENV_DIR:-venv}"
if [ -x "$VENV_DIR/bin/gunicorn" ]; then
    GUNICORN_BIN="$VENV_DIR/bin/gunicorn"
elif command -v gunicorn >/dev/null 2>&1; then
    GUNICORN_BIN="$(command -v gunicorn)"
else
    echo "gunicorn introuvable. Installez les dépendances de prod : pip install -e '.[prod]'" >&2
    exit 1
fi

mkdir -p data logs

exec "$GUNICORN_BIN" -c gunicorn.conf.py kartotek_master.wsgi:app
