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
    page_title = gettext("À propos")

    return render_template(
        "info/index.html",
        page_title=page_title,
        og_title=page_title,
        og_description=gettext("À propos de KartoTek et de cette collection de cartes postales."),
        og_type="website",
    )
