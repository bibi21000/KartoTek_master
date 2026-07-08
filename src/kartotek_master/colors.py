"""
Attribue une couleur stable à chaque serveur, dérivée d'un hash de son URL.

Déterministe : un même server_url donne toujours la même couleur, quel
que soit l'ordre d'insertion ou le nombre de serveurs connus — utile
pour que la légende et les marqueurs restent cohérents d'un rafraîchissement
à l'autre, y compris quand un nouveau serveur apparaît.
"""

import colorsys
import hashlib


def color_for_server(server_url: str) -> str:
    digest = hashlib.md5(server_url.encode("utf-8")).hexdigest()
    hue = (int(digest[:8], 16) % 360) / 360.0
    # Saturation/luminosité fixes : des teintes bien saturées, lisibles
    # aussi bien sur fond sombre que sur les tuiles OpenStreetMap.
    r, g, b = colorsys.hls_to_rgb(hue, 0.58, 0.62)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
