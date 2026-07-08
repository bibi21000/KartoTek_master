"""
Blueprint status : page d'état des synchronisations, pour vérifier
rapidement la date et le résultat du dernier poll de chaque serveur.

Volontairement non liée depuis le reste du site (pas dans le header, pas
dans le sitemap) : accès direct par son URL uniquement. En renfort, la
réponse porte un en-tête X-Robots-Tag: noindex et /status/ est exclue via
robots.txt (voir le blueprint seo) — ça n'empêche pas d'y accéder
directement, mais évite qu'elle se retrouve indexée ou explorée par des
robots qui respectent ces règles.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_babel import gettext

from .. import db

bp = Blueprint("status", __name__, template_folder="../templates")


@bp.route("/status/")
def index():
    servers = db.list_servers_state()
    page_title = gettext("État des synchronisations")
    response = render_template(
        "status/index.html",
        page_title=page_title,
        servers=servers,
        total_points=db.count_points(),
    )
    headers = {"X-Robots-Tag": "noindex, nofollow"}
    return response, 200, headers
