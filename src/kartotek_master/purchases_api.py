"""
kartotek_master/purchases_api.py - Routes HTTP pour la validation
centralisée des achats in-app ("déblocage pro").

  POST /api/v1/purchase/verify  → appelé par l'appli mobile juste après
                                    un achat (ou pour un "restore
                                    purchases"). Revalide TOUJOURS
                                    auprès du store, voir
                                    kartotek_master.purchases.
  GET  /api/v1/purchase/status  → relit le dernier statut mémorisé,
                                    sans re-solliciter le store (appelé
                                    plus fréquemment, ex. à chaque
                                    lancement de l'appli).
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request
from flask_limiter.util import get_remote_address

from .limiter import limiter
from .purchases import PurchaseVerificationError, get_entitlement_status, verify_purchase

bp = Blueprint("purchases_api", __name__)


def _purchase_device_key() -> str:
    """
    Clé de limite basée sur le device_id envoyé plutôt que l'IP -- même
    raisonnement que push_api._push_token_key (plusieurs appareils
    peuvent partager une même IP publique).
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id") or request.args.get("device_id") or "").strip()
    return f"purchase-device:{device_id[:128]}" if device_id else get_remote_address()


@bp.route("/api/v1/purchase/verify", methods=["POST"])
@limiter.limit("10 per minute;60 per hour", key_func=_purchase_device_key)
def purchase_verify():
    """
    Corps JSON :
      {
        "device_id": "...",          -- identifiant stable côté appli (pas l'IDFA/IDFV,
                                          voir recommandations vie privée Apple/Google : un
                                          UUID généré et stocké par l'appli suffit)
        "platform": "android"|"ios",
        "product_id": "kartotek_pro_unlock",
        "purchase_ref": "..."        -- purchase_token (Android) ou transaction_id (iOS)
      }

    200 { "active": true, "status": "active", "product_id": "...", "platform": "..." }
    400 { "error": "..." }   -- champ manquant/invalide
    402 { "error": "..." }   -- reçu vérifié mais achat non valide (annulé/remboursé/en attente)
    429                      -- trop de vérifications pour ce device_id
    501 { "error": "..." }   -- plateforme non configurée côté master
    502 { "error": "..." }   -- store injoignable / réponse inattendue
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    try:
        result = verify_purchase(
            device_id=str(data.get("device_id", "")).strip(),
            platform=str(data.get("platform", "")).strip().lower(),
            product_id=str(data.get("product_id", "")).strip(),
            purchase_ref=str(data.get("purchase_ref", "")).strip(),
        )
    except PurchaseVerificationError as exc:
        return jsonify({"error": str(exc)}), exc.status_code

    return jsonify(result)


@bp.route("/api/v1/purchase/status")
@limiter.limit("30 per minute;300 per hour", key_func=_purchase_device_key)
def purchase_status():
    """
    Query string : device_id (obligatoire), product_id (obligatoire).

    200 { "active": true, "status": "active", "product_id": "...",
          "platform": "...", "verified_at": "..." }
    200 { "active": false, "status": "unknown" }  -- jamais vérifié par ce master
    400 { "error": "..." }  -- device_id/product_id manquant
    """
    device_id = request.args.get("device_id", "").strip()
    product_id = request.args.get("product_id", "").strip()
    if not device_id or not product_id:
        return jsonify({"error": "device_id et product_id sont obligatoires"}), 400

    status = get_entitlement_status(device_id, product_id)
    if status is None:
        return jsonify({"active": False, "status": "unknown", "product_id": product_id})

    return jsonify(status)
