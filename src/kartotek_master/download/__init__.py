"""
Blueprint download : page permettant de télécharger l'application KartoTek
(paquets, installeurs...) déposés par l'administrateur dans le répertoire
configuré via `download_dir` (section [flask] de config.conf, chemin
relatif à la racine du projet ou absolu). Accessible depuis la page
/info/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, send_from_directory
from flask_babel import gettext
from werkzeug.utils import secure_filename

from ..limiter import limiter

bp = Blueprint("download", __name__, template_folder="../templates")

# Limite appliquée aux deux routes de ce blueprint (liste + téléchargement
# effectif). Valeur fixe volontairement simple ; à ajuster ici si besoin
# de la rendre configurable via config.conf.
DOWNLOAD_RATE_LIMIT = "30 per minute"


def _download_dir() -> Path:
    return Path(current_app.config["DOWNLOAD_DIR"])


def _human_size(num_bytes: int) -> str:
    """Formate une taille en octets vers une unité lisible (Ko, Mo, Go...)."""
    size = float(num_bytes)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            if unit == "o":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


@bp.route("/download/")
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def index():
    """Liste les fichiers disponibles dans le répertoire de téléchargement."""
    page_title = gettext("Télécharger l'application Kartotek")
    download_dir = _download_dir()

    files = []
    if download_dir.is_dir():
        # Path.iterdir() ne renvoie jamais "." ni ".." (contrairement à un
        # `ls -a` par exemple) ; on exclut simplement les sous-répertoires
        # pour ne garder que les fichiers proposés au téléchargement.
        for entry in sorted(download_dir.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            stat = entry.stat()
            files.append(
                {
                    "name": entry.name,
                    "size_human": _human_size(stat.st_size),
                    # Format ISO 8601 en UTC : converti côté navigateur dans
                    # la locale de l'utilisateur (voir le script en bas de
                    # download/index.html), avec un rendu serveur en repli
                    # si JS est désactivé (filtre `localdatetime`).
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )

    return render_template(
        "download/index.html",
        page_title=page_title,
        og_title=page_title,
        og_description=gettext("Téléchargements de l'application KartoTek."),
        og_type="website",
        files=files,
    )


@bp.route("/download/<path:filename>")
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def get_file(filename: str):
    """Sert un fichier du répertoire de téléchargement (interdit toute évasion de chemin)."""
    download_dir = _download_dir()
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        abort(404)
    file_path = download_dir / safe_name
    if not file_path.is_file():
        abort(404)
    return send_from_directory(download_dir, safe_name, as_attachment=True)
