"""
kartotek_master/push_api.py - Routes HTTP pour le registre push centralisé.

  POST /api/v1/push/register    → public, appelé par l'app mobile (une
                                    seule fois pour tous les serveurs,
                                    plus besoin de s'inscrire sur chaque
                                    flpostcards individuellement)
  POST /api/v1/push/unregister  → public, idem
  POST /api/v1/push/notify      → interne, appelé par flpostcards.push_watch
                                    de chaque serveur dès qu'il détecte
                                    une nouvelle carte. Protégé par un
                                    secret partagé (PUSH_NOTIFY_SECRET),
                                    PAS destiné à l'app mobile.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .limiter import limiter
from .push import PushRegistrationError, notify_new_card, register_token, unregister_token

bp = Blueprint("push_api", __name__)


def _push_token_key() -> str:
    """
    Clé de rate limit basée sur le token envoyé plutôt que l'IP :
    plusieurs téléphones peuvent partager la même IP publique (NAT
    d'un opérateur mobile).
    """
    from flask_limiter.util import get_remote_address

    data: dict[str, Any] = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    return f"push-token:{token[:64]}" if token else get_remote_address()


@bp.route("/api/v1/push/register", methods=["POST"])
@limiter.limit("20 per minute;200 per hour", key_func=_push_token_key)
def push_register():
    """
    Enregistre (ou met à jour) le token push d'un appareil, une seule
    fois pour l'ensemble du réseau KartoTek — pas besoin de s'inscrire
    séparément sur chaque flpostcards.

    Corps JSON :
      { "token": "...", "platform": "android"|"ios", "lat": 46.7, "lon": 5.6, "radius": 500 }

    200 { "status": "ok", "platform": "...", "radius": 500.0 }
    400 { "error": "..." }
    429                       — trop de ré-inscriptions pour ce token
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    try:
        entry = register_token(
            token=str(data.get("token", "")).strip(),
            platform=str(data.get("platform", "")).strip().lower(),
            lat=data.get("lat"),
            lon=data.get("lon"),
            radius=data.get("radius"),
        )
    except PushRegistrationError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"status": "ok", "platform": entry["platform"], "radius": entry["radius"]})


@bp.route("/api/v1/push/unregister", methods=["POST"])
@limiter.limit("20 per minute;200 per hour", key_func=_push_token_key)
def push_unregister():
    """Corps JSON : { "token": "..." } → 200 { "status": "ok", "removed": true|false }"""
    data: dict[str, Any] = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if not token:
        return jsonify({"error": "token est obligatoire"}), 400

    removed = unregister_token(token)
    return jsonify({"status": "ok", "removed": removed})


@bp.route("/api/v1/push/notify", methods=["POST"])
@limiter.limit("120 per minute")
def push_notify():
    """
    Route INTERNE : appelée par chaque flpostcards (flpostcards.push_watch)
    dès qu'il détecte une nouvelle carte, pas par l'app mobile.

    Authentification : en-tête `X-Kartotek-Push-Secret`, comparé (en
    temps constant, hmac.compare_digest) à `[push] notify_secret` de
    config.conf. Un secret UNIQUE partagé par tous les flpostcards pour
    l'instant (simple à opérer) plutôt qu'une clé par serveur — voir
    docs/07-PUSH_NOTIFICATIONS.md pour l'amélioration possible (clé par
    serveur, si le besoin de révoquer un serveur individuellement se
    présente).

    Corps JSON :
      {
        "server_url": "https://server1.kartotek.eu",
        "card_id": "123",
        "title": "Vue du village",
        "lat": 46.749,
        "lon": 5.620
      }

    200 { "status": "ok", "targeted": N, "fcm_sent": ..., "apns_sent": ... }
    401                       — secret manquant/incorrect
    400 { "error": "..." }    — champ manquant/invalide
    """
    expected_secret = current_app.config.get("PUSH_NOTIFY_SECRET")
    if not expected_secret:
        # Section [push] non configurée sur le master : personne ne
        # devrait appeler cette route. On refuse plutôt que d'accepter
        # sans authentification.
        return jsonify({"error": "push non configuré côté master"}), 503

    provided_secret = request.headers.get("X-Kartotek-Push-Secret", "")
    if not hmac.compare_digest(provided_secret, expected_secret):
        return jsonify({"error": "secret invalide"}), 401

    data: dict[str, Any] = request.get_json(silent=True) or {}
    server_url = str(data.get("server_url", "")).strip()
    card_id = str(data.get("card_id", "")).strip()
    title = str(data.get("title", "")).strip()
    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lon obligatoires (float)"}), 400

    if not card_id:
        return jsonify({"error": "card_id est obligatoire"}), 400

    result = notify_new_card(server_url=server_url, card_id=card_id, title=title, lat=lat, lon=lon)
    return jsonify({"status": "ok", **result})
