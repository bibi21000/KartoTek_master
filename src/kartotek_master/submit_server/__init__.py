"""
Blueprint submit_server : formulaire « Référencer mon serveur », accessible
depuis /info/. Permet à un tiers de proposer l'ajout de son serveur
(nom, URL, description) à conf/servers.json — envoyé par email à l'admin
via SMTP, exactement comme le formulaire de contact (voir
kartotek_master.contact). L'ajout effectif à servers.json reste manuel :
ce formulaire ne fait que notifier, il ne modifie rien automatiquement.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext

bp = Blueprint("submit_server", __name__, template_folder="../templates")

_MAX_LEN = {"name": 100, "url": 300, "description": 1000}


def _smtp_configured() -> bool:
    """Le formulaire n'est activé que si un serveur SMTP est configuré (comme /contact/)."""
    return bool(current_app.config.get("SMTP_HOST")) and bool(
        current_app.config.get("CONTACT_EMAIL")
    )


def _send_email(name: str, url: str, description: str) -> None:
    config = current_app.config

    contact_email = config["CONTACT_EMAIL"]
    smtp_host = config["SMTP_HOST"]
    smtp_port = config.get("SMTP_PORT", 587)
    smtp_user = config.get("SMTP_USER", "")
    smtp_password = config.get("SMTP_PASSWORD", "")
    smtp_security = (config.get("SMTP_SECURITY") or "starttls").lower()

    msg = EmailMessage()
    msg["Subject"] = f"[KartoTek] Proposition de serveur : {name}"
    msg["From"] = formataddr((name, smtp_user or contact_email))
    msg["To"] = contact_email
    msg.set_content(
        "Nouvelle proposition de serveur à référencer sur KartoTek :\n\n"
        f"Nom : {name}\n"
        f"URL : {url}\n"
        f"Description : {description or 'non renseignée'}\n"
    )

    if smtp_security == "ssl":
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)

    try:
        if smtp_security == "starttls":
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_password)
        server.send_message(msg)
    finally:
        server.quit()


@bp.route("/info/referencer/", methods=["GET", "POST"])
def index():
    """Formulaire de proposition de serveur : nom, URL, description."""
    if not _smtp_configured():
        from flask import abort

        abort(404)

    page_title = gettext("Référencer mon serveur")
    errors: dict[str, str] = {}
    values = {"name": "", "url": "", "description": ""}

    if request.method == "POST":
        # Honeypot anti-spam : champ caché en CSS, normalement vide.
        if request.form.get("website"):
            flash(gettext("Merci, votre proposition a bien été envoyée !"), "success")
            return redirect(url_for("submit_server.index"))

        for field in values:
            values[field] = request.form.get(field, "").strip()[: _MAX_LEN[field]]

        if not values["name"]:
            errors["name"] = gettext("Le nom du serveur est obligatoire.")
        if not values["url"]:
            errors["url"] = gettext("L'URL du serveur est obligatoire.")
        else:
            parsed = urlparse(values["url"])
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors["url"] = gettext(
                    "L'URL n'est pas valide (doit commencer par http:// ou https://)."
                )
        if not values["description"]:
            errors["description"] = gettext("Une courte description est nécessaire.")

        if not errors:
            try:
                _send_email(values["name"], values["url"], values["description"])
            except Exception:
                current_app.logger.exception("Échec de l'envoi de la proposition de serveur")
                errors["_global"] = gettext(
                    "L'envoi de la proposition a échoué. Réessayez plus tard."
                )
            else:
                flash(gettext("Merci, votre proposition a bien été envoyée !"), "success")
                return redirect(url_for("submit_server.index"))

    return render_template(
        "submit_server/index.html",
        page_title=page_title,
        errors=errors,
        values=values,
        og_title=page_title,
        og_description=gettext("Proposez votre serveur pour l'ajouter à KartoTek."),
        og_type="website",
    )
