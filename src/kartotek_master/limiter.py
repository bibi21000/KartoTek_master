"""
Instance partagée de Flask-Limiter.

Créée ici sans application liée (initialisation différée, voir
https://flask-limiter.readthedocs.io/en/stable/#deferred-initialization)
afin que les blueprints (ex. download) puissent importer `limiter` et
décorer leurs routes avec `@limiter.limit(...)` sans import circulaire
avec app.py. L'initialisation réelle (storage_uri, key_prefix...) est
faite dans `create_app()` via `limiter.init_app(app)`, une fois la
configuration lue depuis config.conf.
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Par défaut on limite par adresse IP du client. Aucune limite globale
# n'est définie ici : chaque route qui doit être protégée porte son
# propre décorateur `@limiter.limit(...)` (voir kartotek_master.download).
limiter = Limiter(key_func=get_remote_address, default_limits=[])
