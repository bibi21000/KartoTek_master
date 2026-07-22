"""
Blueprint download : page permettant de télécharger l'application KartoTek
(paquets, installeurs...) déposés par l'administrateur dans le répertoire
configuré via `download_dir` (section [flask] de config.conf, chemin
relatif à la racine du projet ou absolu). Accessible depuis la page
/info/.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import markdown as markdown_lib
from flask import Blueprint, abort, current_app, render_template, send_from_directory
from flask_babel import gettext, get_locale
from werkzeug.utils import secure_filename

from ..limiter import limiter

bp = Blueprint("download", __name__, template_folder="../templates")

# Limite appliquée aux deux routes de ce blueprint (liste + téléchargement
# effectif). Valeur fixe volontairement simple ; à ajuster ici si besoin
# de la rendre configurable via config.conf.
DOWNLOAD_RATE_LIMIT = "30 per minute"

# Nom du fichier affiché comme description au-dessus de la liste, plutôt
# que proposé au téléchargement. Dépend de "python-markdown" (pip install
# Markdown) pour la conversion en HTML.
#
# Des variantes localisées peuvent être déposées à côté, sous la forme
# README_<langue>.md (ex. README_fr.md, README_en.md) : celle correspondant
# à la langue courante de l'utilisateur est utilisée si elle existe, sinon
# on retombe sur README.md. Peu importe la ou les variantes présentes,
# aucune n'est proposée au téléchargement (voir _README_FILENAME_RE).
README_NAME = "README.md"
README_BASE_NAME = "README"
_README_FILENAME_RE = re.compile(
    r"^README(?:_[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?)?\.md$", re.IGNORECASE
)

_MARKDOWN_EXTENSIONS = ["extra", "sane_lists", "toc"]

_TITLE_RE = re.compile(r"(?m)^#[ \t]+(.+?)[ \t]*$")


class _LocalizedMarkdownDoc:
    """Document Markdown localisé (ex. KartoTek_User_Guide_fr.md) affiché
    sous forme de lien (avec pour libellé son titre, extrait de son premier
    titre Markdown de niveau 1 `# ...`) plutôt que proposé au téléchargement.

    Des variantes localisées peuvent être déposées sous la forme
    <base_name>_<langue>.md (ex. KartoTek_User_Guide_fr.md) : celle
    correspondant à la langue courante de l'utilisateur est utilisée si elle
    existe, avec repli sur l'anglais (<base_name>_en.md) puis sur la variante
    par défaut (<base_name>.md). Aucune variante n'est jamais proposée au
    téléchargement, et son rendu HTML est mis en cache par fichier tant que
    celui-ci n'est pas modifié (comparaison de mtime)."""

    def __init__(self, base_name: str, endpoint: str):
        self.base_name = base_name
        self.endpoint = endpoint
        self.filename_re = re.compile(
            rf"^{re.escape(base_name)}(?:_[A-Za-z]{{2,3}}(?:[-_][A-Za-z]{{2,4}})?)?\.md$",
            re.IGNORECASE,
        )
        # {str(path): (mtime, html, title, {image_names})}
        self._cache: dict[str, tuple[float, str, str, set[str]]] = {}

    def filename_candidates(self, locale_code: str) -> list[str]:
        candidates = []
        if locale_code:
            candidates.append(f"{self.base_name}_{locale_code}.md")
            base = re.split(r"[-_]", locale_code)[0]
            if base and base.lower() != locale_code.lower():
                candidates.append(f"{self.base_name}_{base}.md")
        candidates.append(f"{self.base_name}_en.md")
        candidates.append(f"{self.base_name}.md")

        seen: set[str] = set()
        ordered: list[str] = []
        for name in candidates:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(name)
        return ordered

    def find_path(self, download_dir: Path, locale_code: str) -> Path | None:
        """Retourne le chemin de la variante correspondant le mieux à
        `locale_code` parmi celles présentes sur disque, ou None."""
        for name in self.filename_candidates(locale_code):
            path = download_dir / name
            if path.is_file():
                return path
        return None

    def render(self, path: Path) -> tuple[str, str, set[str]] | None:
        """Rend en HTML le fichier Markdown situé à `path`, avec mise en
        cache : le rendu n'est refait que si la mtime du fichier a changé
        depuis le dernier appel. Retourne (html, titre, {images}), ou None
        si le fichier est illisible."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cache_key = str(path)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            _, html, title, images = cached
            return html, title, images

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        html = markdown_lib.markdown(text, extensions=_MARKDOWN_EXTENSIONS)
        title = _extract_title(text, path.stem)
        images = _referenced_image_names(text)
        self._cache[cache_key] = (mtime, html, title, images)
        return html, title, images

    def all_filenames(self, download_dir: Path) -> set[str]:
        """Noms de toutes les variantes présentes dans le répertoire de
        téléchargement, quelle que soit leur langue."""
        if not download_dir.is_dir():
            return set()
        return {
            entry.name
            for entry in download_dir.iterdir()
            if entry.is_file() and self.filename_re.match(entry.name)
        }

    def all_images(self, download_dir: Path, filenames: set[str]) -> set[str]:
        """Union des noms d'images référencées par toutes les variantes
        présentes (pas seulement celle de la langue courante), afin
        qu'aucune ne soit proposée au téléchargement ni servie en pièce
        jointe, quelle que soit la langue depuis laquelle elle est affichée."""
        images: set[str] = set()
        for name in filenames:
            rendered = self.render(download_dir / name)
            if rendered:
                images |= rendered[2]
        return images


# Nom du fichier affiché comme description au-dessus de la liste, plutôt
# que proposé au téléchargement. Dépend de "python-markdown" (pip install
# Markdown) pour la conversion en HTML.
#
# Des variantes localisées peuvent être déposées à côté, sous la forme
# README_<langue>.md (ex. README_fr.md, README_en.md) : celle correspondant
# à la langue courante de l'utilisateur est utilisée si elle existe, sinon
# on retombe sur README.md. Peu importe la ou les variantes présentes,
# aucune n'est proposée au téléchargement (voir _README_FILENAME_RE).
README_NAME = "README.md"
README_BASE_NAME = "README"
_README_FILENAME_RE = re.compile(
    r"^README(?:_[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?)?\.md$", re.IGNORECASE
)

# Guide utilisateur et guide d'installation : chacun affiché comme un lien
# (avec pour libellé son titre) au-dessus de la liste des téléchargements,
# avec repli de langue et cache de rendu (voir _LocalizedMarkdownDoc).
USER_GUIDE = _LocalizedMarkdownDoc("KartoTek_User_Guide", endpoint="download.guide")
INSTALLATION_GUIDE = _LocalizedMarkdownDoc(
    "KartoTek_Installation_Guide", endpoint="download.installation_guide"
)

# Repère les images référencées dans le README, que ce soit en syntaxe
# Markdown `![alt](chemin "titre")` ou en HTML brut `<img src="chemin">`.
# Les URLs externes (http(s)://, //) sont ignorées : seuls les fichiers
# locaux au répertoire de téléchargement nous intéressent.
_IMAGE_REF_RE = re.compile(
    r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+["\'][^"\']*["\'])?\s*\)'
    r'|<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _download_dir() -> Path:
    return Path(current_app.config["DOWNLOAD_DIR"])


def _readme_filename_candidates(locale_code: str) -> list[str]:
    """Liste ordonnée des noms de fichier à essayer pour le README dans la
    langue courante : la version localisée en priorité (ex. README_fr.md),
    puis, si le code contient une région (ex. "fr-CA"), sa langue de base
    (README_fr.md), et enfin la version par défaut (README.md)."""
    candidates = []
    if locale_code:
        candidates.append(f"{README_BASE_NAME}_{locale_code}.md")
        base = re.split(r"[-_]", locale_code)[0]
        if base and base.lower() != locale_code.lower():
            candidates.append(f"{README_BASE_NAME}_{base}.md")
    candidates.append(README_NAME)
    return candidates


def _referenced_image_names(markdown_text: str) -> set[str]:
    """Extrait les noms de fichiers image référencés localement dans un texte
    Markdown (chemins relatifs simples uniquement ; les sous-répertoires
    éventuels sont ignorés car ce blueprint ne sert que des fichiers à plat)."""
    names: set[str] = set()
    for match in _IMAGE_REF_RE.finditer(markdown_text):
        target = match.group(1) or match.group(2)
        if not target:
            continue
        if "://" in target or target.startswith("//"):
            continue  # URL externe, on ne l'exclut pas de la liste
        target = target.split("#", 1)[0].split("?", 1)[0]
        name = Path(target).name
        if name:
            names.add(name)
    return names


def _readme_html_and_images(download_dir: Path, locale_code: str) -> tuple[str | None, set[str]]:
    """Lit la version du README correspondant à `locale_code` (ou, à défaut,
    README.md) dans le répertoire de téléchargement, la convertit en HTML et
    retourne également les noms des images qu'elle référence (pour les
    exclure de la liste et les servir en ligne plutôt qu'en pièce jointe)."""
    for name in _readme_filename_candidates(locale_code):
        readme_path = download_dir / name
        if not readme_path.is_file():
            continue
        try:
            text = readme_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        html = markdown_lib.markdown(text, extensions=_MARKDOWN_EXTENSIONS)
        return html, _referenced_image_names(text)
    return None, set()



def _extract_title(markdown_text: str, fallback: str) -> str:
    """Extrait le titre d'un document depuis son premier titre Markdown de
    niveau 1 (`# Titre`) ; à défaut, retourne `fallback`."""
    match = _TITLE_RE.search(markdown_text)
    if match:
        title = match.group(1).strip()
        if title:
            return title
    return fallback


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

    readme_html = None
    guide_title = None
    install_guide_title = None
    excluded_names: set[str] = set()
    files = []
    if download_dir.is_dir():
        locale_code = str(get_locale())
        readme_html, readme_images = _readme_html_and_images(download_dir, locale_code)

        install_filenames = INSTALLATION_GUIDE.all_filenames(download_dir)
        install_images = INSTALLATION_GUIDE.all_images(download_dir, install_filenames)
        install_path = INSTALLATION_GUIDE.find_path(download_dir, locale_code)
        if install_path is not None:
            rendered = INSTALLATION_GUIDE.render(install_path)
            if rendered is not None:
                install_guide_title = rendered[1]

        guide_filenames = USER_GUIDE.all_filenames(download_dir)
        guide_images = USER_GUIDE.all_images(download_dir, guide_filenames)
        guide_path = USER_GUIDE.find_path(download_dir, locale_code)
        if guide_path is not None:
            rendered = USER_GUIDE.render(guide_path)
            if rendered is not None:
                guide_title = rendered[1]

        # Le README (quelle que soit sa variante linguistique présente :
        # README.md, README_fr.md, README_en.md...) et les guides
        # utilisateur/installation (KartoTek_User_Guide.md,
        # KartoTek_Installation_Guide_fr.md...), ainsi que les images
        # qu'ils illustrent, ne doivent pas apparaître dans la liste des
        # fichiers proposés au téléchargement : le README fait partie de la
        # description affichée au-dessus, et chaque guide est accessible via
        # son propre lien.
        excluded_names = (
            readme_images
            | guide_images
            | guide_filenames
            | install_images
            | install_filenames
            | {
                entry.name
                for entry in download_dir.iterdir()
                if entry.is_file() and _README_FILENAME_RE.match(entry.name)
            }
        )

        # Path.iterdir() ne renvoie jamais "." ni ".." (contrairement à un
        # `ls -a` par exemple) ; on exclut simplement les sous-répertoires
        # pour ne garder que les fichiers proposés au téléchargement.
        for entry in sorted(download_dir.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file() or entry.name in excluded_names:
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
        readme_html=readme_html,
        guide_title=guide_title,
        install_guide_title=install_guide_title,
    )


@bp.route("/download/guide")
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def guide():
    """Affiche le rendu HTML du guide utilisateur correspondant à la langue
    courante (avec repli sur l'anglais puis sur la variante par défaut)."""
    return _render_guide_page(USER_GUIDE)


@bp.route("/download/installation-guide")
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def installation_guide():
    """Affiche le rendu HTML du guide d'installation correspondant à la
    langue courante (avec repli sur l'anglais puis sur la variante par
    défaut)."""
    return _render_guide_page(INSTALLATION_GUIDE)


def _render_guide_page(doc: _LocalizedMarkdownDoc):
    download_dir = _download_dir()
    if not download_dir.is_dir():
        abort(404)

    doc_path = doc.find_path(download_dir, str(get_locale()))
    if doc_path is None:
        abort(404)

    rendered = doc.render(doc_path)
    if rendered is None:
        abort(404)
    guide_html, guide_title, _ = rendered

    return render_template(
        "download/guide.html",
        page_title=guide_title,
        og_title=guide_title,
        og_type="article",
        guide_html=guide_html,
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

    # Les images illustrant le README ou les guides (utilisateur,
    # installation) sont affichées inline dans leurs pages respectives (via
    # leur rendu HTML) : elles doivent donc être servies sans en-tête
    # Content-Disposition: attachment, sinon le navigateur ouvrirait une
    # boîte de dialogue de téléchargement au lieu de les afficher.
    _, readme_images = _readme_html_and_images(download_dir, str(get_locale()))
    guide_images = USER_GUIDE.all_images(download_dir, USER_GUIDE.all_filenames(download_dir))
    install_images = INSTALLATION_GUIDE.all_images(
        download_dir, INSTALLATION_GUIDE.all_filenames(download_dir)
    )
    as_attachment = safe_name not in (readme_images | guide_images | install_images)
    return send_from_directory(download_dir, safe_name, as_attachment=as_attachment)
