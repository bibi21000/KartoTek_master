"""
Blueprint info : page de présentation, accessible depuis le logo affiché
en haut de chaque page. Contient un lien vers le formulaire de contact.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_babel import gettext

bp = Blueprint("info", __name__, template_folder="../templates")


@bp.route("/info/")
def index():
    """Page d'information / présentation du site."""
    page_title = gettext("Informations")

    return render_template(
        "info/index.html",
        page_title=page_title,
        og_title=gettext("KartoTek"),
        og_description=gettext("KartoTek et cette collection de cartes postales."),
        og_type="website",
    )


@bp.route("/info/serveurs/")
def servers():
    """
    Liste des serveurs connus (servers.json), affichée en tableau. Le tri
    (par distance ou alphabétique) est calculé côté client une fois la
    position GPS de l'utilisateur connue (ou non) — voir le JS de
    info/servers.html, qui consomme /api/v1/servers.
    """
    page_title = gettext("Serveurs")

    return render_template(
        "info/servers.html",
        page_title=page_title,
        og_title=page_title,
        og_description=gettext("Liste des serveurs KartoTek connus."),
        og_type="website",
    )
