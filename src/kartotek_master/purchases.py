"""
kartotek_master/purchases.py - Validation côté serveur des achats in-app
("déblocage pro") pour Google Play et l'App Store.

Contexte : le déverrouillage de la version pro est un achat unique, au
niveau de l'application (pas d'un serveur KartoTek Web en particulier).
Comme pour le registre push (voir kartotek_master.push), c'est le master
qui centralise la vérification et la mémorisation de l'entitlement — pas
chaque flpostcards — pour n'avoir à distribuer les credentials store
(clé service account Google, clé App Store Connect) qu'à un seul endroit.

Principe :
  - L'appli mobile envoie le reçu d'achat (purchase_token Android /
    transaction_id iOS) une fois, juste après l'achat (ou au lancement,
    pour "restore purchases").
  - Le master revalide TOUJOURS auprès du store correspondant (jamais
    confiance dans un simple "j'ai acheté" envoyé par le client : un
    appareil root/jailbreaké peut mentir) avant d'accorder l'entitlement.
  - Le résultat (actif/révoqué) est mémorisé dans la table
    ``purchase_entitlements`` (voir kartotek_master.db), pour que
    GET /api/v1/purchase/status réponde sans re-solliciter le store à
    chaque lancement de l'appli.

Ce que ce module NE fait PAS (limites connues, à traiter séparément) :
  - Pas d'écoute des notifications temps réel des stores (Google Play
    Real-time Developer Notifications / App Store Server Notifications
    V2) pour détecter un remboursement ENTRE deux lancements de l'appli
    par l'utilisateur concerné. Tant que l'appli ne rappelle pas
    /api/v1/purchase/verify ou /status, un remboursement ne sera vu
    qu'au prochain appel. Acceptable pour un achat unique bon marché,
    mais à muscler (webhook dédié) si ça devient sensible.
  - La vérification Apple ci-dessous valide la chaîne de certificats du
    JWS (x5c) jusqu'à la racine Apple fournie en configuration
    (APPSTORE_ROOT_CA_FILE) : téléchargez le certificat officiel
    "Apple Root CA - G3" depuis https://www.apple.com/certificateauthority/
    et référencez son chemin en configuration -- ce module ne l'embarque
    PAS en dur dans le code (un certificat de confiance ne doit pas
    dépendre d'un copier-coller dans une source, mais d'un fichier que
    vous contrôlez et pouvez mettre à jour).
  - Non testé contre les endpoints réels Google Play / App Store dans
    cet environnement (pas de credentials, pas d'accès réseau sortant
    disponible ici) -- à valider en sandbox avant mise en production,
    comme indiqué pour push.py.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from flask import current_app

from . import db

_VALID_PLATFORMS = {"android", "ios"}


class PurchaseVerificationError(Exception):
    """
    Levée pour tout achat qui ne peut pas être validé.

    ``status_code`` indique le code HTTP à renvoyer par la route
    appelante (voir kartotek_master.purchases_api) :
      400 -- requête mal formée (champ manquant/invalide)
      402 -- reçu vérifié avec succès auprès du store, mais achat non
             valide (annulé, remboursé, en attente)
      501 -- vérification non configurée côté master pour cette plateforme
      502 -- store injoignable ou réponse inattendue
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Point d'entrée commun
# ---------------------------------------------------------------------------

def verify_purchase(
    *, device_id: str, platform: str, product_id: str, purchase_ref: str,
) -> dict:
    """
    Vérifie un achat auprès du store correspondant et mémorise
    l'entitlement obtenu (voir db.upsert_purchase_entitlement).

    ``purchase_ref`` est le purchase_token Android ou le transaction_id
    iOS -- champ générique côté appelant pour ne pas dupliquer la route
    HTTP par plateforme (voir POST /api/v1/purchase/verify).

    Retourne {"active": bool, "status": "active"|"revoked"|"pending",
    "product_id": ..., "platform": ...}.

    Lève PurchaseVerificationError (voir sa docstring pour le mapping
    des status_code) -- ne renvoie jamais une entitlement "active" sans
    l'avoir réellement vérifiée.
    """
    if not device_id:
        raise PurchaseVerificationError("device_id est obligatoire")
    if platform not in _VALID_PLATFORMS:
        raise PurchaseVerificationError(f"platform doit être l'un de {sorted(_VALID_PLATFORMS)}")
    if not product_id:
        raise PurchaseVerificationError("product_id est obligatoire")
    if not purchase_ref:
        raise PurchaseVerificationError("purchase_ref est obligatoire (purchase_token ou transaction_id)")

    if platform == "android":
        status = _verify_google_play(product_id=product_id, purchase_token=purchase_ref)
    else:
        status = _verify_appstore(product_id=product_id, transaction_id=purchase_ref)

    db.upsert_purchase_entitlement(
        device_id=device_id,
        product_id=product_id,
        platform=platform,
        status=status,
        purchase_ref=purchase_ref,
    )

    current_app.logger.info(
        "purchases : %s/%s vérifié pour device=%s -> status=%s",
        platform, product_id, device_id, status,
    )

    return {
        "active": status == "active",
        "status": status,
        "product_id": product_id,
        "platform": platform,
    }


def get_entitlement_status(device_id: str, product_id: str) -> dict | None:
    """
    Relit le dernier statut mémorisé (sans re-solliciter le store) --
    utilisé par GET /api/v1/purchase/status. Retourne None si cet
    appareil n'a jamais fait vérifier cet achat par ce master.
    """
    row = db.get_purchase_entitlement(device_id, product_id)
    if row is None:
        return None
    return {
        "active": row["status"] == "active",
        "status": row["status"],
        "product_id": row["product_id"],
        "platform": row["platform"],
        "verified_at": row["verified_at"],
    }


# ---------------------------------------------------------------------------
# Google Play — Android Publisher API v3
# ---------------------------------------------------------------------------

def _google_play_access_token() -> str | None:
    account_file = current_app.config.get("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE")
    if not account_file:
        return None

    cached = getattr(current_app, "_google_play_credentials", None)
    if cached is not None and cached.valid:
        return cached.token

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        account_file, scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    credentials.refresh(GoogleAuthRequest())
    current_app._google_play_credentials = credentials
    return credentials.token


def _verify_google_play(*, product_id: str, purchase_token: str) -> str:
    """
    Interroge l'API Android Publisher pour un produit géré (achat
    unique, pas un abonnement -- voir purchases.products.get) et
    acquitte l'achat si nécessaire (un achat non acquitté sous 3 jours
    est automatiquement remboursé par Google, voir leur documentation
    "Acknowledge in-app purchases").

    Retourne "active", "pending" ou "revoked" (voir purchaseState :
    0 = acheté, 1 = annulé, 2 = en attente, voir doc Google).
    """
    package_name = current_app.config.get("GOOGLE_PLAY_PACKAGE_NAME")
    access_token = _google_play_access_token()
    if not package_name or not access_token:
        raise PurchaseVerificationError(
            "vérification Google Play non configurée côté master", status_code=501
        )

    import requests

    timeout = current_app.config.get("PURCHASE_HTTP_TIMEOUT_S", 10.0)
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        f"{package_name}/purchases/products/{product_id}/tokens/{purchase_token}"
    )
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise PurchaseVerificationError(f"Google Play injoignable : {exc}", status_code=502) from exc

    if resp.status_code == 404:
        # Purchase token inconnu de Google (faux, périmé, ou d'une
        # autre application) -- à distinguer d'un vrai remboursement.
        raise PurchaseVerificationError("purchase_token inconnu de Google Play", status_code=402)
    if resp.status_code != 200:
        raise PurchaseVerificationError(
            f"Google Play a répondu {resp.status_code} : {resp.text[:300]}", status_code=502
        )

    try:
        payload = resp.json()
        purchase_state = int(payload["purchaseState"])
    except (ValueError, KeyError, TypeError) as exc:
        raise PurchaseVerificationError(
            f"réponse Google Play invalide : {resp.text[:300]}", status_code=502
        ) from exc

    status = {0: "active", 1: "revoked", 2: "pending"}.get(purchase_state, "revoked")

    if status == "active" and int(payload.get("acknowledgementState", 1)) == 0:
        _acknowledge_google_play(
            package_name=package_name, product_id=product_id,
            purchase_token=purchase_token, access_token=access_token,
        )

    return status


def _acknowledge_google_play(
    *, package_name: str, product_id: str, purchase_token: str, access_token: str,
) -> None:
    """
    Best-effort : un échec ici n'invalide pas l'achat déjà confirmé
    comme "purchased" ci-dessus, mais est loggé bruyamment -- sans
    acquittement sous 3 jours, Google rembourse automatiquement
    l'utilisateur, donc un échec répété doit être surveillé.
    """
    import requests

    timeout = current_app.config.get("PURCHASE_HTTP_TIMEOUT_S", 10.0)
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        f"{package_name}/purchases/products/{product_id}/tokens/{purchase_token}:acknowledge"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.post(url, headers=headers, json={}, timeout=timeout)
        if resp.status_code not in (200, 204):
            current_app.logger.warning(
                "purchases : échec acquittement Google Play (%d) : %s",
                resp.status_code, resp.text[:300],
            )
    except requests.RequestException as exc:
        current_app.logger.warning("purchases : échec acquittement Google Play (réseau) : %s", exc)


# ---------------------------------------------------------------------------
# App Store — App Store Server API (JWS signé, vérifié via x5c)
# ---------------------------------------------------------------------------

def _apple_provider_token() -> str | None:
    key_file = current_app.config.get("APPSTORE_KEY_FILE")
    key_id = current_app.config.get("APPSTORE_KEY_ID")
    issuer_id = current_app.config.get("APPSTORE_ISSUER_ID")
    bundle_id = current_app.config.get("APPSTORE_BUNDLE_ID")
    if not (key_file and key_id and issuer_id and bundle_id):
        return None

    cached = getattr(current_app, "_appstore_token", None)
    cached_at = getattr(current_app, "_appstore_token_ts", 0)
    if cached and (time.time() - cached_at) < 25 * 60:
        return cached

    import jwt as pyjwt
    from pathlib import Path

    private_key = Path(key_file).read_text(encoding="utf-8")
    now = int(time.time())
    token = pyjwt.encode(
        {
            "iss": issuer_id,
            "iat": now,
            "exp": now + 30 * 60,
            "aud": "appstoreconnect-v1",
            "bid": bundle_id,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )
    current_app._appstore_token = token
    current_app._appstore_token_ts = time.time()
    return token


def _apple_root_ca():
    """
    Charge la racine de confiance Apple depuis APPSTORE_ROOT_CA_FILE
    (voir la docstring du module : à télécharger vous-même depuis
    https://www.apple.com/certificateauthority/, "Apple Root CA - G3").
    """
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    root_ca_file = current_app.config.get("APPSTORE_ROOT_CA_FILE")
    if not root_ca_file:
        return None

    from pathlib import Path

    raw = Path(root_ca_file).read_bytes()
    try:
        return x509.load_pem_x509_certificate(raw, default_backend())
    except ValueError:
        return x509.load_der_x509_certificate(raw, default_backend())


def _verify_jws_chain(signed_payload: str) -> dict:
    """
    Vérifie un JWS signé par Apple (App Store Server API /
    App Store Server Notifications V2) :

      1. Décode l'en-tête JOSE pour en extraire ``x5c`` (chaîne de
         certificats : certificat feuille puis intermédiaire Apple,
         encodés en base64 standard -- PAS base64url).
      2. Vérifie que le certificat intermédiaire est bien signé par la
         racine Apple configurée (APPSTORE_ROOT_CA_FILE), et que le
         certificat feuille est bien signé par l'intermédiaire --
         établit la confiance jusqu'à la racine sans dépendre d'un
         magasin de certificats système.
      3. Vérifie la signature ES256 du JWS avec la clé publique du
         certificat feuille.

    Retourne le payload décodé (dict). Lève PurchaseVerificationError
    si la chaîne ou la signature ne vérifient pas.
    """
    import jwt as pyjwt
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.exceptions import InvalidSignature

    try:
        header_b64 = signed_payload.split(".", 1)[0]
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
        x5c = header["x5c"]
    except (IndexError, ValueError, KeyError) as exc:
        raise PurchaseVerificationError("JWS Apple malformé (en-tête/x5c)", status_code=502) from exc

    if len(x5c) < 2:
        raise PurchaseVerificationError("JWS Apple : chaîne de certificats incomplète", status_code=502)

    try:
        leaf_cert = x509.load_der_x509_certificate(base64.b64decode(x5c[0]), default_backend())
        intermediate_cert = x509.load_der_x509_certificate(base64.b64decode(x5c[1]), default_backend())
    except ValueError as exc:
        raise PurchaseVerificationError("JWS Apple : certificat invalide", status_code=502) from exc

    root_cert = _apple_root_ca()
    if root_cert is None:
        raise PurchaseVerificationError(
            "APPSTORE_ROOT_CA_FILE non configuré côté master", status_code=501
        )

    def _signed_by(child, parent) -> bool:
        try:
            parent.public_key().verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(child.signature_hash_algorithm),
            )
            return True
        except InvalidSignature:
            return False

    if not _signed_by(intermediate_cert, root_cert):
        raise PurchaseVerificationError(
            "JWS Apple : certificat intermédiaire non signé par la racine Apple configurée",
            status_code=502,
        )
    if not _signed_by(leaf_cert, intermediate_cert):
        raise PurchaseVerificationError(
            "JWS Apple : certificat feuille non signé par l'intermédiaire", status_code=502
        )

    try:
        payload = pyjwt.decode(
            signed_payload,
            key=leaf_cert.public_key(),
            algorithms=["ES256"],
            options={"verify_exp": False, "verify_aud": False},
        )
    except pyjwt.PyJWTError as exc:
        raise PurchaseVerificationError(f"signature JWS Apple invalide : {exc}", status_code=502) from exc

    return payload


def _verify_appstore(*, product_id: str, transaction_id: str) -> str:
    """
    Récupère puis vérifie la transaction auprès de l'App Store Server
    API, et en déduit le statut (voir revocationDate : présent =
    remboursée/annulée par Apple ou le support client).
    """
    provider_token = _apple_provider_token()
    if not provider_token:
        raise PurchaseVerificationError(
            "vérification App Store non configurée côté master", status_code=501
        )

    import requests

    sandbox = current_app.config.get("APPSTORE_USE_SANDBOX", False)
    host = "api.storekit-sandbox.itunes.apple.com" if sandbox else "api.storekit.itunes.apple.com"
    timeout = current_app.config.get("PURCHASE_HTTP_TIMEOUT_S", 10.0)
    url = f"https://{host}/inApps/v1/transactions/{transaction_id}"
    headers = {"Authorization": f"Bearer {provider_token}"}

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise PurchaseVerificationError(f"App Store injoignable : {exc}", status_code=502) from exc

    if resp.status_code == 404:
        raise PurchaseVerificationError("transaction_id inconnu de l'App Store", status_code=402)
    if resp.status_code != 200:
        raise PurchaseVerificationError(
            f"App Store a répondu {resp.status_code} : {resp.text[:300]}", status_code=502
        )

    try:
        signed_transaction_info = resp.json()["signedTransactionInfo"]
    except (ValueError, KeyError) as exc:
        raise PurchaseVerificationError("réponse App Store invalide", status_code=502) from exc

    payload: dict[str, Any] = _verify_jws_chain(signed_transaction_info)

    if payload.get("productId") != product_id:
        raise PurchaseVerificationError(
            "productId de la transaction Apple ne correspond pas à celui demandé",
            status_code=402,
        )

    if payload.get("revocationDate") is not None:
        return "revoked"
    return "active"
