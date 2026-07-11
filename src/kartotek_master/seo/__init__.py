"""
Blueprint seo : tout ce qui concerne l'indexation par les moteurs de
recherche.

- /<filename>.html|.txt|.xml : sert les fichiers de vérification de
  propriété déposés dans HTMLDIR/verification/ (Google Search Console,
  Yandex via .html/.txt ; Bing via BingSiteAuth.xml). Enregistré sans
  préfixe : ces fichiers doivent être accessibles exactement à la racine
  du domaine, comme l'exigent ces outils.
- /sitemap.xml : généré dynamiquement à partir des pages du site et de la
  liste des serveurs connus (chacun expose sa collection sur /map/).
- /robots.txt : autorise l'indexation générale, référence le sitemap, et
  interdit explicitement /status/ (qui n'est de toute façon jamais lié
  nulle part).
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, abort, current_app, render_template, request, send_from_directory

from .. import db

bp = Blueprint("seo", __name__, template_folder="../templates")


@bp.route("/<string:filename>")
def verification_file(filename: str):
    """
    Sert les fichiers de vérification de propriété de site déposés dans
    htmldir/verification/ (Google Search Console, Yandex via .html/.txt ;
    Bing via BingSiteAuth.xml). Seuls les fichiers .html, .txt et .xml sont
    autorisés, et uniquement depuis ce répertoire dédié — aucun autre
    fichier du serveur n'est exposé.
    Usage : déposer le fichier fourni par le moteur de recherche dans
    htmldir/verification/ puis accéder à /<nom-du-fichier>.
    """
    if not filename.endswith((".html", ".txt", ".xml")):
        abort(404)
    verification_dir = Path(current_app.config["HTMLDIR"]) / "verification"
    if not verification_dir.exists():
        abort(404)
    file_path = verification_dir / filename
    if not file_path.exists():
        abort(404)
    if filename.endswith(".html"):
        mimetype = "text/html"
    elif filename.endswith(".xml"):
        mimetype = "application/xml"
    else:
        mimetype = "text/plain"
    return send_from_directory(verification_dir, filename, mimetype=mimetype)


@bp.route("/sitemap.xml")
def sitemap():
    """
    Sitemap dynamique : les pages du site lui-même, plus une entrée par
    serveur connu (chacun expose sa propre collection sur `<url>/map/`) —
    le site agissant comme un annuaire de collections de cartes postales.
    La page /status/ n'y figure volontairement pas.
    """
    contact_enabled = bool(current_app.config.get("SMTP_HOST")) and bool(
        current_app.config.get("CONTACT_EMAIL")
    )

    servers = []
    for row in db.list_servers_state():
        url = (row.get("server_url") or "").rstrip("/")
        if not url:
            continue
        last_sync = row.get("last_sync")
        if last_sync:
            # Normalise en ISO 8601 sans les microsecondes (attendu par le
            # protocole sitemap).
            last_sync = last_sync.split(".")[0]
        servers.append({"url": url, "last_sync": last_sync})

    xml = render_template(
        "sitemap.xml",
        url_root=request.url_root,
        contact_enabled=contact_enabled,
        servers=servers,
    )
    return Response(xml, mimetype="application/xml")


@bp.route("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /status/",
        f"Sitemap: {request.url_root}sitemap.xml",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")
