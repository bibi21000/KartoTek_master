"""
Blueprint privacy : politique de confidentialité de kartotek.eu (le
master). Toujours active (contrairement à contact, qui reste désactivée
tant que [contact] contact_email/smtp_host ne sont pas renseignés) :
Apple et Google exigent une URL de politique de confidentialité
accessible publiquement pour toute app qui enregistre des tokens push,
indépendamment de la configuration SMTP du master.

kartotek.eu est le SEUL composant du réseau KartoTek à voir passer les
tokens push (voir kartotek_master.push / push_api) : chaque flpostcards
se contente de signaler "nouvelle carte ici" au master, qui gère seul
le registre des appareils et l'envoi FCM/APNs. Cette page est donc la
référence pour tout ce qui concerne les notifications ; elle renvoie
vers la politique de chaque flpostcards pour le reste (comptes
gestionnaires, repérages de terrain, photos "similaires").

Configuration (config.conf, section [contact] réutilisée, plus une
section [privacy] optionnelle) :

    [privacy]
    operator_name = Jean Dupont
    last_updated = 2026-07-26
    push_token_retention_days = 400   # au-delà, un token jamais renouvelé est considéré mort
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template
from flask_babel import gettext

bp = Blueprint("privacy", __name__, template_folder="../templates")


@bp.route("/privacy/")
def index():
    """
    Politique de confidentialité du master — toujours accessible, pour
    rester référençable depuis l'appli mobile (écran "à propos", avant
    même la sélection d'un serveur, voir capabilities.capabilities) et
    depuis les fiches App Store / Play Store.
    """
    config = current_app.config

    operator_name = config.get("PRIVACY_OPERATOR_NAME") or gettext(
        "L'équipe qui exploite kartotek.eu"
    )
    contact_email = config.get("PRIVACY_OPERATOR_CONTACT") or config.get("CONTACT_EMAIL")
    last_updated = config.get("PRIVACY_LAST_UPDATED")
    push_configured = bool(config.get("PUSH_NOTIFY_SECRET"))
    fcm_configured = bool(config.get("FCM_PROJECT_ID"))
    apns_configured = bool(config.get("APNS_KEY_ID"))
    matomo_enabled = bool(config.get("SITE_MATOMO"))

    page_title = gettext("Politique de confidentialité")

    return render_template(
        "privacy/index.html",
        page_title=page_title,
        operator_name=operator_name,
        contact_email=contact_email,
        last_updated=last_updated,
        push_configured=push_configured,
        fcm_configured=fcm_configured,
        apns_configured=apns_configured,
        matomo_enabled=matomo_enabled,
        og_title=page_title,
        og_description=gettext(
            "Quelles données kartotek.eu collecte et pourquoi, notamment pour "
            "les notifications push."
        ),
        og_type="website",
    )
