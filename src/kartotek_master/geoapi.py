"""
Blueprint des endpoints géo de kartotek_master : /api/v1/points (affichage
de la carte, points bruts ou clusters) ainsi que bounds / nearby /
next-update, calqués sur les endpoints équivalents exposés par chaque
serveur de cartes postales (voir flpostcards.blueprints.api), mais côté
kartotek_master ces trois derniers opèrent sur PLUSIEURS serveurs à la fois.

Paramètre commun aux trois routes :
  servers : liste de server_url séparés par des virgules, indiquant quels
            serveurs (parmi ceux connus, voir conf/servers.json) inclure
            dans l'agrégation. Absent ou vide => tous les serveurs connus.
            Toute URL qui ne correspond à aucun serveur connu est
            silencieusement ignorée (on ne veut ni interroger, ni exposer
            un serveur non configuré).

Optimisation : contrairement aux endpoints d'origine, ces routes NE FONT
JAMAIS d'appel HTTP sortant. Le poller (kartotek_master.poller) tient déjà
à jour, en base SQLite, l'ensemble des points GPS et des bounding box de
chaque serveur configuré ; ces trois routes se contentent d'interroger
cette base (déjà un cache), filtrée sur les serveurs demandés. Un appel à
/api/v1/nearby avec 20 serveurs sélectionnés ne déclenche donc pas 20
requêtes HTTP vers les serveurs distants, mais une poignée de requêtes
SQL locales — au prix d'une fraîcheur limitée à l'intervalle du poller
(`interval_minutes` dans config.conf, 15 min par défaut), comme pour
/api/v1/points et /api/status qui reposent déjà sur ce même cache.
"""

from __future__ import annotations

import math

from flask import Blueprint, current_app, jsonify, request

from . import db
from .colors import color_for_server
from .geo import haversine_km

bp = Blueprint("geoapi", __name__)

# Mêmes constantes que côté serveur de cartes postales (flpostcards/blueprints/api.py)
_POLL_MIN_S = 10
_POLL_MAX_S = 300
_MAX_RADIUS_M = 50_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_servers():
    """
    Lit le paramètre de requête `servers` (liste de server_url séparés
    par des virgules) et le restreint aux serveurs effectivement connus
    en base (servers_state, alimentée par servers.json via le poller).

    Retourne (servers, unknown) :
      servers : liste de server_url valides à utiliser pour le filtre
                (liste vide => pas de filtre = tous les serveurs connus)
      unknown : sous-ensemble demandé mais non reconnu (juste pour info
                dans la réponse JSON, ne bloque jamais la requête)
    """
    raw = request.args.get("servers", "")
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    if not requested:
        return [], []

    known = db.list_known_server_urls()
    valid = [s for s in requested if s in known]
    unknown = [s for s in requested if s not in known]
    return valid, unknown


def _bbox_for_radius(lat: float, lon: float, radius_m: float):
    """
    Bounding box approximative (large, non précise) englobant le cercle
    de rayon `radius_m` autour de (lat, lon) — sert uniquement de
    pré-filtre SQL bon marché avant le calcul haversine exact fait
    ensuite en Python sur un sous-ensemble déjà réduit de points.
    """
    delta_lat = radius_m / 111_000.0
    # Se protège d'une division par ~0 près des pôles (hors sujet ici,
    # mais évite un crash si des données aberrantes existaient).
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    delta_lon = radius_m / (111_000.0 * cos_lat)
    return (lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon)


def _server_name(server_url: str, states_by_url: dict) -> str:
    state = states_by_url.get(server_url)
    return (state or {}).get("name") or server_url


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/api/v1/points")
def points():
    """
    Renvoie les points GPS visibles dans la bbox demandée (tous serveurs
    confondus, sans filtre `servers` — c'est la route utilisée par la
    carte pour l'affichage global).
    - zoom >= cluster_zoom_threshold : points bruts (limités).
    - sinon : clusters agrégés en SQL (évite de transférer des dizaines
      de milliers de marqueurs au navigateur).

    Paramètres attendus : bbox=minLon,minLat,maxLon,maxLat & zoom=<int>
    """
    cluster_zoom_threshold = current_app.config["CLUSTER_ZOOM_THRESHOLD"]
    max_points = current_app.config["MAX_POINTS_RETURNED"]

    bbox = request.args.get("bbox", "")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except (ValueError, AttributeError):
        return jsonify({"error": "paramètre bbox invalide, attendu: minLon,minLat,maxLon,maxLat"}), 400

    try:
        zoom = int(request.args.get("zoom", 6))
    except ValueError:
        zoom = 6

    limit = min(max_points, int(request.args.get("limit", max_points)))

    if zoom >= cluster_zoom_threshold:
        pts = db.query_points_raw(min_lon, min_lat, max_lon, max_lat, limit)
        for p in pts:
            p["color"] = color_for_server(p["server_url"])
        return jsonify({"type": "points", "zoom": zoom, "data": pts})

    # Plus le zoom est faible, plus les cellules de regroupement sont grandes
    precision = max(1, min(5, zoom // 2))
    clusters = db.query_points_clustered(min_lon, min_lat, max_lon, max_lat, precision, limit)
    for c in clusters:
        c["color"] = color_for_server(c["server_url"])
    return jsonify({"type": "clusters", "zoom": zoom, "data": clusters})


@bp.route("/api/v1/bounds")
def bounds():
    """
    Zone GPS couverte par l'ensemble des serveurs sélectionnés (rectangle
    englobant les bounding box individuelles, déjà connues en base via
    /api/v1/bounds côté chaque serveur distant, récupérées par le poller).

    Paramètres de requête :
      servers : (optionnel) server_url séparés par des virgules

    Réponse JSON :
      {
        "count": 3,                 -- nombre de serveurs pris en compte (avec bounds connues)
        "bounds": {"min_lat", "max_lat", "min_lon", "max_lon"} | null,
        "servers": [
          {"url", "name", "color", "bounds": {...} | null},
          ...
        ],
        "unknown_servers": [...]    -- servers demandés mais non reconnus
      }
    """
    requested, unknown = _resolve_servers()
    states = db.list_servers_state()
    if requested:
        states = [s for s in states if s["server_url"] in requested]

    servers_out = []
    min_lat = max_lat = min_lon = max_lon = None
    with_bounds = 0

    for s in states:
        entry = {
            "url": s["server_url"],
            "name": s.get("name") or s["server_url"],
            "color": color_for_server(s["server_url"]),
            "bounds": None,
        }
        b_min_lat, b_max_lat = s.get("bounds_min_lat"), s.get("bounds_max_lat")
        b_min_lon, b_max_lon = s.get("bounds_min_lon"), s.get("bounds_max_lon")
        if None not in (b_min_lat, b_max_lat, b_min_lon, b_max_lon):
            entry["bounds"] = {
                "min_lat": b_min_lat, "max_lat": b_max_lat,
                "min_lon": b_min_lon, "max_lon": b_max_lon,
            }
            with_bounds += 1
            min_lat = b_min_lat if min_lat is None else min(min_lat, b_min_lat)
            max_lat = b_max_lat if max_lat is None else max(max_lat, b_max_lat)
            min_lon = b_min_lon if min_lon is None else min(min_lon, b_min_lon)
            max_lon = b_max_lon if max_lon is None else max(max_lon, b_max_lon)
        servers_out.append(entry)

    combined = None
    if with_bounds:
        combined = {
            "min_lat": min_lat, "max_lat": max_lat,
            "min_lon": min_lon, "max_lon": max_lon,
        }

    return jsonify({
        "count": with_bounds,
        "bounds": combined,
        "servers": servers_out,
        "unknown_servers": unknown,
    })


@bp.route("/api/v1/nearby")
def nearby():
    """
    Cartes postales (tous serveurs sélectionnés confondus) dans un rayon
    autour d'une position GPS, calculées à partir des points déjà
    synchronisés en base par le poller (aucun appel réseau ici).

    Paramètres de requête (lat/lon/radius obligatoires) :
      lat, lon : position (float)
      radius   : rayon de recherche en mètres (float, max 50 000)
      servers  : (optionnel) server_url séparés par des virgules

    Réponse JSON :
      {
        "count": N,
        "servers_used": [...], "unknown_servers": [...],
        "cards": [
          {"id", "server_url", "server_name", "color", "lat", "lon", "distance_m"},
          ...
        ]  -- triées par distance croissante
      }
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), _MAX_RADIUS_M)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon et radius sont obligatoires (float)"}), 400

    requested, unknown = _resolve_servers()
    states_by_url = {s["server_url"]: s for s in db.list_servers_state()}
    servers_used = requested or list(states_by_url.keys())

    min_lat, max_lat, min_lon, max_lon = _bbox_for_radius(lat, lon, radius)
    candidates = db.query_points_for_servers(
        servers_used, min_lat=min_lat, max_lat=max_lat, min_lon=min_lon, max_lon=max_lon
    )

    results = []
    for p in candidates:
        dist_km = haversine_km(lat, lon, p["lat"], p["lon"])
        dist_m = dist_km * 1000.0
        if dist_m <= radius:
            server_url = p["server_url"]
            results.append({
                "id": p["external_id"],
                "server_url": server_url,
                "server_name": _server_name(server_url, states_by_url),
                "color": color_for_server(server_url),
                "lat": p["lat"],
                "lon": p["lon"],
                "distance_m": round(dist_m, 1),
            })

    results.sort(key=lambda x: x["distance_m"])

    return jsonify({
        "count": len(results),
        "servers_used": servers_used,
        "unknown_servers": unknown,
        "cards": results,
    })


@bp.route("/api/v1/next-update")
def next_update():
    """
    Délai recommandé (en secondes) avant le prochain appel à
    /api/v1/nearby, calculé sur les mêmes points en cache (aucun appel
    réseau), en tenant compte de tous les serveurs sélectionnés.

    Paramètres de requête :
      lat, lon, radius : comme pour /api/v1/nearby
      speed    : vitesse de déplacement en m/s (float, défaut 0 = immobile)
      servers  : (optionnel) server_url séparés par des virgules
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), _MAX_RADIUS_M)
        speed = max(float(request.args.get("speed", 0)), 0.0)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon, radius (et optionnellement speed) sont obligatoires"}), 400

    requested, unknown = _resolve_servers()

    if speed <= 0:
        return jsonify({
            "next_update_s": _POLL_MAX_S,
            "reason": "immobile",
            "unknown_servers": unknown,
        })

    known = db.list_known_server_urls()
    servers_used = requested or list(known)

    min_lat, max_lat, min_lon, max_lon = _bbox_for_radius(lat, lon, radius)
    candidates = db.query_points_for_servers(
        servers_used, min_lat=min_lat, max_lat=max_lat, min_lon=min_lon, max_lon=max_lon
    )

    min_dist_in_radius = None
    for p in candidates:
        dist_m = haversine_km(lat, lon, p["lat"], p["lon"]) * 1000.0
        if dist_m <= radius:
            if min_dist_in_radius is None or dist_m < min_dist_in_radius:
                min_dist_in_radius = dist_m

    effective_distance = min_dist_in_radius if min_dist_in_radius is not None else radius
    remaining = max(radius - effective_distance, 0)
    delay = max(_POLL_MIN_S, min(remaining / speed, _POLL_MAX_S))

    return jsonify({
        "next_update_s": round(delay, 1),
        "reason": "moving",
        "speed_ms": speed,
        "radius_m": radius,
        "nearest_card_m": round(min_dist_in_radius, 1) if min_dist_in_radius is not None else None,
        "servers_used": servers_used,
        "unknown_servers": unknown,
    })
