"""
Blueprint contact : formulaire de contact envoyant un email via SMTP.

Configuration requise (section [contact] de conf/config.conf) :
  contact_email  : adresse destinataire des messages
  smtp_host      : serveur SMTP (la page est désactivée si vide/absent)
  smtp_port      : port SMTP (587 = starttls, 465 = ssl)
  smtp_user      : utilisateur SMTP
  smtp_password  : mot de passe SMTP
  smtp_security  : "starttls" ou "ssl"
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext

bp = Blueprint("contact", __name__, template_folder="../templates")

_MAX_LEN = {"name": 100, "email": 200, "subject": 200, "message": 5000}


def _smtp_configured() -> bool:
    """La page de contact n'est activée que si un serveur SMTP est configuré."""
    return bool(current_app.config.get("SMTP_HOST")) and bool(
        current_app.config.get("CONTACT_EMAIL")
    )


def _send_email(name: str, sender_email: str, subject: str, message: str) -> None:
    """
    Envoie le message via SMTP, avec Reply-To pointant vers l'expéditeur
    du formulaire (pour pouvoir répondre directement depuis sa boîte mail).
    Lève une exception en cas d'échec (gérée par l'appelant).
    """
    config = current_app.config

    contact_email = config["CONTACT_EMAIL"]
    smtp_host = config["SMTP_HOST"]
    smtp_port = config.get("SMTP_PORT", 587)
    smtp_user = config.get("SMTP_USER", "")
    smtp_password = config.get("SMTP_PASSWORD", "")
    smtp_security = (config.get("SMTP_SECURITY") or "starttls").lower()

    full_subject = subject.strip() or gettext("Nouveau message depuis le site")

    msg = EmailMessage()
    msg["Subject"] = f"[Cartes postales] {full_subject}"
    msg["From"] = formataddr((name, smtp_user or contact_email))
    msg["To"] = contact_email
    if sender_email:
        msg["Reply-To"] = formataddr((name, sender_email))
    msg.set_content(
        f"{message}\n\n---\n"
        f"De : {name} <{sender_email or 'non renseigné'}>\n"
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


@bp.route("/contact/", methods=["GET", "POST"])
def index():
    """Formulaire de contact : nom, email, sujet, message."""
    if not _smtp_configured():
        from flask import abort

        abort(404)

    page_title = gettext("Contact")
    errors: dict[str, str] = {}
    values = {"name": "", "email": "", "subject": "", "message": ""}

    if request.method == "POST":
        # Honeypot anti-spam : champ caché en CSS, normalement vide.
        # Un bot qui remplit aveuglément tous les champs s'y fera piéger.
        if request.form.get("website"):
            # Réponse silencieuse : on fait croire au bot que ça a marché
            flash(gettext("Votre message a bien été envoyé, merci !"), "success")
            return redirect(url_for("contact.index"))

        for field in values:
            values[field] = request.form.get(field, "").strip()[: _MAX_LEN[field]]

        if not values["name"]:
            errors["name"] = gettext("Le nom est obligatoire.")
        if not values["email"]:
            errors["email"] = gettext("L'adresse email est obligatoire.")
        elif "@" not in values["email"] or "." not in values["email"].split("@")[-1]:
            errors["email"] = gettext("L'adresse email n'est pas valide.")
        if not values["message"]:
            errors["message"] = gettext("Le message ne peut pas être vide.")

        if not errors:
            try:
                _send_email(
                    values["name"], values["email"], values["subject"], values["message"]
                )
            except Exception:
                current_app.logger.exception("Échec de l'envoi du message de contact")
                errors["_global"] = gettext(
                    "L'envoi du message a échoué. Réessayez plus tard."
                )
            else:
                flash(gettext("Votre message a bien été envoyé, merci !"), "success")
                return redirect(url_for("contact.index"))

    return render_template(
        "contact/index.html",
        page_title=page_title,
        errors=errors,
        values=values,
        og_title=page_title,
        og_description=gettext("Contactez-moi à propos de ma collection de cartes postales."),
        og_type="website",
    )
