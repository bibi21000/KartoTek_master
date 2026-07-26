"""
kartotek_master/push.py - Registre centralisé des tokens push (FCM/APNs)
et envoi des notifications "nouvelle carte à proximité".

Contexte : détecter et notifier depuis chaque `flpostcards` (une
implémentation par serveur) obligeait à distribuer les mêmes credentials
FCM/APNs sur N serveurs. Ici, un seul endroit détient les clés et fait
l'envoi effectif :

  - `POST /api/v1/push/register` / `unregister` (routes publiques, voir
    kartotek_master.push_api) : l'app mobile s'inscrit UNE FOIS auprès du
    master, pas auprès de chaque serveur.
  - `POST /api/v1/push/notify` (route interne, protégée par un secret
    partagé, voir kartotek_master.push_api) : chaque `flpostcards`
    l'appelle dès qu'il détecte lui-même une nouvelle carte (c'est LUI
    qui a `cdate`/`title` instantanément, voir flpostcards.push_watch),
    et délègue au master le filtrage par rayon + l'envoi FCM/APNs.

Stockage : table SQLite `push_registrations` (voir kartotek_master.db) —
cohérent avec le reste du master (pas de nouveau mécanisme de fichiers
verrouillés à maintenir en plus de gps_points/servers_state).

Dépendances Python additionnelles (mêmes que la version précédente côté
flpostcards, à déplacer dans les requirements du master) :
  - google-auth (FCM HTTP v1)
  - httpx[http2] (APNs)
  - PyJWT[crypto] (jeton provider APNs, ES256)

Non testé contre les endpoints réels FCM/APNs dans cet environnement
(pas de credentials, pas d'accès réseau sortant disponible) — à valider
en sandbox avant mise en production, voir docs/07-PUSH_NOTIFICATIONS.md.
"""

from __future__ import annotations

import time
from typing import Any

from flask import current_app

from . import db
from .geo import haversine_km

_VALID_PLATFORMS = {"ios", "android"}
_DEFAULT_MAX_RADIUS_M = 5_000.0


class PushRegistrationError(ValueError):
    """Levée pour toute entrée invalide dans register_token()."""


# ---------------------------------------------------------------------------
# Registre (register / unregister)
# ---------------------------------------------------------------------------

def register_token(token: str, platform: str, lat: Any, lon: Any, radius: Any) -> dict:
    """Voir la docstring de la route POST /api/v1/push/register (push_api.py) pour le contrat."""
    if not token or len(token) > 4096:
        raise PushRegistrationError("token manquant ou invalide")
    if platform not in _VALID_PLATFORMS:
        raise PushRegistrationError(f"platform doit être l'un de {sorted(_VALID_PLATFORMS)}")
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        raise PushRegistrationError("lat et lon sont obligatoires (float)")
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        raise PushRegistrationError("lat/lon hors limites")
    try:
        radius_f = float(radius)
    except (TypeError, ValueError):
        raise PushRegistrationError("radius est obligatoire (float, mètres)")
    if radius_f <= 0:
        raise PushRegistrationError("radius doit être positif")

    max_radius = current_app.config.get("PUSH_MAX_RADIUS_M", _DEFAULT_MAX_RADIUS_M)
    radius_f = min(radius_f, max_radius)

    db.upsert_push_registration(token, platform, lat_f, lon_f, radius_f)
    return {"token": token, "platform": platform, "lat": lat_f, "lon": lon_f, "radius": radius_f}


def unregister_token(token: str) -> bool:
    if not token:
        return False
    return db.delete_push_registration(token)


# ---------------------------------------------------------------------------
# Notification d'une carte nouvellement ajoutée (appelée par la route
# interne POST /api/v1/push/notify, elle-même appelée par un flpostcards)
# ---------------------------------------------------------------------------

def notify_new_card(*, server_url: str, card_id: str, title: str, lat: float, lon: float) -> dict:
    """
    Filtre les inscriptions dont le rayon couvre (lat, lon), envoie via
    FCM (Android) / APNs (iOS). `server_url` sert uniquement au log (utile
    pour diagnostiquer quel flpostcards a déclenché quoi).

    Retourne {"targeted": N, "fcm_sent": ..., "apns_sent": ...}.
    """
    max_radius = current_app.config.get("PUSH_MAX_RADIUS_M", _DEFAULT_MAX_RADIUS_M)
    candidates = db.query_push_registrations_near(lat, lon, max_radius)
    targets = [
        r for r in candidates
        if haversine_km(lat, lon, r["lat"], r["lon"]) * 1000.0 <= r["radius_m"]
    ]
    if not targets:
        return {"targeted": 0, "fcm_sent": 0, "apns_sent": 0}

    android_tokens = [r["token"] for r in targets if r["platform"] == "android"]
    ios_tokens = [r["token"] for r in targets if r["platform"] == "ios"]

    push_title = current_app.config.get("PUSH_TITLE", "Nouvelle carte postale à proximité")

    fcm_result = _send_fcm(android_tokens, title=push_title, body=title, card_id=card_id)
    apns_result = _send_apns(ios_tokens, title=push_title, body=title, card_id=card_id)

    invalid = set(fcm_result.get("invalid_tokens", [])) | set(apns_result.get("invalid_tokens", []))
    if invalid:
        db.delete_push_registrations(invalid)

    current_app.logger.info(
        "push: carte %s (%s) notifiée à %d appareil(s) (android=%d ok/%d, ios=%d ok/%d)",
        card_id, server_url, len(targets),
        fcm_result.get("sent", 0), len(android_tokens),
        apns_result.get("sent", 0), len(ios_tokens),
    )

    return {
        "targeted": len(targets),
        "fcm_sent": fcm_result.get("sent", 0),
        "apns_sent": apns_result.get("sent", 0),
    }


# ---------------------------------------------------------------------------
# FCM (Android) — API HTTP v1  (identique à l'implémentation flpostcards
# initiale, déplacée ici sans changement de logique)
# ---------------------------------------------------------------------------

def _fcm_access_token() -> str | None:
    account_file = current_app.config.get("FCM_SERVICE_ACCOUNT_FILE")
    if not account_file:
        return None

    cached = getattr(current_app, "_fcm_credentials", None)
    if cached is not None and cached.valid:
        return cached.token

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        account_file, scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )
    credentials.refresh(GoogleAuthRequest())
    current_app._fcm_credentials = credentials
    return credentials.token


def _send_fcm(tokens: list[str], *, title: str, body: str, card_id: str) -> dict:
    if not tokens:
        return {"sent": 0, "invalid_tokens": []}

    project_id = current_app.config.get("FCM_PROJECT_ID")
    access_token = _fcm_access_token()
    if not project_id or not access_token:
        current_app.logger.debug("push: FCM non configuré, %d token(s) android ignorés", len(tokens))
        return {"sent": 0, "invalid_tokens": []}

    import requests

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; UTF-8"}
    timeout = current_app.config.get("PUSH_HTTP_TIMEOUT_S", 10.0)

    sent = 0
    invalid_tokens: list[str] = []
    for token in tokens:
        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {"card_id": card_id, "type": "new_card"},
            }
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            current_app.logger.warning("push: échec envoi FCM (réseau) : %s", exc)
            continue

        if resp.status_code == 200:
            sent += 1
        elif resp.status_code == 404:
            invalid_tokens.append(token)
        else:
            current_app.logger.warning(
                "push: FCM a renvoyé %d pour un token : %s", resp.status_code, resp.text[:300]
            )

    return {"sent": sent, "invalid_tokens": invalid_tokens}


# ---------------------------------------------------------------------------
# APNs (iOS) — HTTP/2, jeton provider (JWT ES256)
# ---------------------------------------------------------------------------

def _apns_provider_token() -> str | None:
    key_file = current_app.config.get("APNS_KEY_FILE")
    key_id = current_app.config.get("APNS_KEY_ID")
    team_id = current_app.config.get("APNS_TEAM_ID")
    if not (key_file and key_id and team_id):
        return None

    cached = getattr(current_app, "_apns_token", None)
    cached_at = getattr(current_app, "_apns_token_ts", 0)
    if cached and (time.time() - cached_at) < 19 * 60:
        return cached

    import jwt as pyjwt
    from pathlib import Path

    private_key = Path(key_file).read_text(encoding="utf-8")
    token = pyjwt.encode(
        {"iss": team_id, "iat": int(time.time())},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )
    current_app._apns_token = token
    current_app._apns_token_ts = time.time()
    return token


def _send_apns(tokens: list[str], *, title: str, body: str, card_id: str) -> dict:
    if not tokens:
        return {"sent": 0, "invalid_tokens": []}

    topic = current_app.config.get("APNS_TOPIC")
    provider_token = _apns_provider_token()
    if not topic or not provider_token:
        current_app.logger.debug("push: APNs non configuré, %d token(s) ios ignorés", len(tokens))
        return {"sent": 0, "invalid_tokens": []}

    import httpx

    sandbox = current_app.config.get("APNS_USE_SANDBOX", False)
    host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
    timeout = current_app.config.get("PUSH_HTTP_TIMEOUT_S", 10.0)

    payload = {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
        "card_id": card_id,
        "type": "new_card",
    }

    sent = 0
    invalid_tokens: list[str] = []
    with httpx.Client(http2=True, timeout=timeout) as client:
        for token in tokens:
            headers = {
                "authorization": f"bearer {provider_token}",
                "apns-topic": topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
            }
            try:
                resp = client.post(f"https://{host}/3/device/{token}", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                current_app.logger.warning("push: échec envoi APNs (réseau) : %s", exc)
                continue

            if resp.status_code == 200:
                sent += 1
            elif resp.status_code == 410:
                invalid_tokens.append(token)
            else:
                current_app.logger.warning(
                    "push: APNs a renvoyé %d pour un token : %s", resp.status_code, resp.text[:300]
                )

    return {"sent": sent, "invalid_tokens": invalid_tokens}
