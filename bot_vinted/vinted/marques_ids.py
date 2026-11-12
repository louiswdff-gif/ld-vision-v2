"""Identifiants de marques Vinted, lus sur la page publique « Marques connues » (`/brands`).

Pourquoi : pour surveiller beaucoup de marques en UNE seule recherche (rapide), il faut que Vinted filtre lui-même sur
`brand_ids[]=…`. Cette page donne l'identifiant de chaque marque courante (lien `/brand/362-carhartt`). Les marques
absentes de cette page (moins courantes) doivent être cochées une fois dans le filtre « Marque » de vinted.fr : l'adresse
obtenue contient alors leur identifiant.
"""

from __future__ import annotations

import re

from bot_vinted.texte import normaliser

_LIEN_MARQUE = re.compile(r"brand(?:\\u002F|\\/|/)(\d+)-([a-z0-9-]+)")


def slugifier(nom: str) -> str:
    """« Levi's » → « levis », « Ralph Lauren » → « ralph-lauren », « Arc'teryx » → « arcteryx »."""
    sans_apostrophe = re.sub(r"['’`]", "", nom)
    return normaliser(sans_apostrophe).replace(" ", "-")


def extraire_ids(texte: str) -> dict[str, int]:
    """{slug: identifiant} pour chaque lien de marque trouvé (HTML et données Next.js décodées)."""
    index: dict[str, int] = {}
    for ident, slug in _LIEN_MARQUE.findall(texte):
        index.setdefault(slug, int(ident))
    return index


def resoudre(noms: list[str], index: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    """(trouvées {nom: identifiant}, introuvables [noms]), dans l'ordre de `noms`, sans doublon d'identifiant."""
    trouvees: dict[str, int] = {}
    manquantes: list[str] = []
    for nom in noms:
        ident = index.get(slugifier(nom))
        if ident is None:
            manquantes.append(nom)
        elif ident not in trouvees.values():
            trouvees[nom] = ident
    return trouvees, manquantes
