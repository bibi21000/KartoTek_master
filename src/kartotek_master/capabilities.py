"""
kartotek_master/capabilities.py - Endpoint de gouvernance de version pour
les clients mobiles, à interroger UNE SEULE FOIS au démarrage de
l'application, AVANT même la sélection d'un serveur KartoTek Web.

Ne pas confondre avec /api/v1/capabilities exposé par chaque flpostcards
(voir flpostcards.blueprints.api.capabilities) : celui-ci décrit les
fonctionnalités propres À UN serveur donné (similar_search, comptes
manager, collections...), et n'a de sens qu'une fois un serveur choisi.
Celui-ci, côté master, ne concerne QUE la gouvernance de version de
l'écosystème KartoTek dans son ensemble (l'app mobile parle au master
avant de parler à n'importe quel flpostcards) : le client peut ainsi
détecter, dès le lancement et avant tout autre appel réseau, qu'il est
trop ancien pour continuer sans risquer un crash silencieux (schéma de
réponse changé, endpoint retiré) sur l'un ou l'autre des serveurs.

Route :
  GET /api/v1/capabilities
"""

from __future__ import annotations

import importlib.resources as importlib_resources
import json
from typing import Any

from flask import Blueprint, current_app, jsonify, url_for

from .limiter import limiter

bp = Blueprint("capabilities", __name__)


def _load_deprecations() -> list[dict[str, Any]]:
    """
    Lit ``deprecations.json``, distribué à l'intérieur du paquet Python
    ``kartotek_master`` lui-même (voir ``pyproject.toml`` ->
    ``[tool.setuptools.package-data]``), sur le même principe que côté
    flpostcards (voir ``flpostcards.blueprints.api._load_deprecations``) :
    une dépréciation est liée au CODE d'une release donnée du master,
    donc versionnée dans git avec lui plutôt que dans un fichier
    d'exploitation modifiable sans redéploiement.

    Absent ou invalide -> liste vide, jamais d'erreur 500 pour un
    simple fichier manquant (utile en particulier en développement, où
    le paquet peut être lancé directement depuis les sources plutôt
    qu'installé via le mécanisme package-data).
    """
    try:
        raw = importlib_resources.files("kartotek_master").joinpath(
            "deprecations.json"
        ).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


@bp.route("/api/v1/capabilities")
# Route de découverte, appelée typiquement une fois au lancement de
# l'app (et mise en cache localement quelques heures côté client) —
# pas besoin d'une limite serrée, mais on en garde une par précaution
# comme pour les endpoints équivalents côté flpostcards.
@limiter.limit("30 per minute;600 per hour")
def capabilities():
    """
    Gouvernance de version pour l'app mobile KartoTek, à appeler une
    seule fois au démarrage (avant sélection d'un serveur), avec mise
    en cache locale de quelques heures côté client — pas besoin de le
    réinterroger à chaque lancement en cas de coupure réseau.

    Ne nécessite aucune authentification et ne révèle aucune donnée
    sensible : uniquement des informations de version/dépréciation.

    Réponse JSON (200) :
      {
        "api_version": {
          "current": "1.3",                 -- version courante de l'API du master (config [app_version]
                                             -- api_version, null si non renseignée : ne pas en déduire une
                                             -- incompatibilité)
          "min_supported_client": "1.0.0",  -- version minimale de l'appli mobile acceptée par l'écosystème
                                             -- KartoTek ; si la version embarquée du client est strictement
                                             -- inférieure, le client DOIT afficher un écran de mise à jour
                                             -- bloquant avant tout autre appel réseau (null = aucun minimum
                                             -- imposé)
          "recommended_client": "1.4.0"     -- version conseillée, à titre informatif (ex : bandeau non
                                             -- bloquant), null si non renseignée
        },
        "force_update": {
          "required": false,                -- true = mise à jour immédiate exigée, indépendamment de
                                             -- min_supported_client (ex : faille de sécurité découverte sur
                                             -- une version déjà supérieure à min_supported_client) ; les deux
                                             -- mécanismes ont des déclencheurs différents et ne doivent pas
                                             -- être fusionnés côté client
          "reason": null,                   -- message à afficher à l'utilisateur si required=true
          "store_url": {
            "ios": null,
            "android": null
          }
        },
        "deprecations": [                   -- informatif, jamais bloquant : à logger/remonter en analytics
                                             -- côté client plutôt qu'à afficher à l'utilisateur. Distribué
                                             -- avec le paquet kartotek_master lui-même (voir
                                             -- _load_deprecations, liste vide si le fichier est absent).
          {
            "endpoint": "GET /api/v1/servers",
            "since": "2027-01-15",
            "removed_after": "2027-06-01",
            "replacement": "GET /api/v2/servers",
            "message": "..."
          }
        ],
        "privacy_policy_url": "https://kartotek.eu/privacy/"
                                             -- URL absolue, toujours renseignée (voir kartotek_master.privacy) :
                                             -- politique de confidentialité du registre push centralisé,
                                             -- interrogeable dès le lancement de l'app, avant la sélection d'un
                                             -- serveur — utile pour l'écran "à propos" et pour la conformité
                                             -- App Store/Play Store, qui exigent ce lien accessible depuis l'app
                                             -- elle-même.
      }

    Compatibilité ascendante : un client qui ne connaît pas encore une
    clé de cette réponse doit l'ignorer silencieusement (ne jamais
    faire d'analyse stricte du schéma) — c'est ce qui permet d'ajouter
    d'autres champs plus tard sans passer, eux, par un cycle de
    dépréciation.
    """
    config = current_app.config

    return jsonify({
        "api_version": {
            "current": config.get("API_VERSION_CURRENT"),
            "min_supported_client": config.get("MIN_SUPPORTED_CLIENT"),
            "recommended_client": config.get("RECOMMENDED_CLIENT"),
        },
        "force_update": {
            "required": bool(config.get("FORCE_UPDATE_REQUIRED", False)),
            "reason": config.get("FORCE_UPDATE_REASON"),
            "store_url": {
                "ios": config.get("STORE_URL_IOS"),
                "android": config.get("STORE_URL_ANDROID"),
            },
        },
        "deprecations": _load_deprecations(),
        "privacy_policy_url": url_for("privacy.index", _external=True),
    })
