"""
Point d'entrée WSGI, utilisé par gunicorn (ou tout autre serveur WSGI) :

    gunicorn -c gunicorn.conf.py kartotek_master.wsgi:app

Importer ce module suffit à créer l'application Flask et à démarrer le
thread de synchronisation en arrière-plan (voir kartotek_master.app).
"""

from .app import app

__all__ = ["app"]
