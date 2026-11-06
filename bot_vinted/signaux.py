"""Signaux « pépite » : annonce mal mise en valeur, vente pressée, lots.

(La marque mal orthographiée / absente du champ est détectée par `marques.py`.)
Tous les textes sont comparés sans accents ni casse. Les listes de mots viennent de config.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot_vinted.config import Signaux
from bot_vinted.marques import CatalogueMarques
from bot_vinted.texte import normaliser, premier_mot_present
from bot_vinted.vinted.modeles import Annonce

_PIECES = (
    r"pieces?|pcs|articles?|items?|pulls?|t shirts?|tshirts?|polos?|vestes?|sweats?|jeans?|chemises?|pantalons?|"
    r"robes?|shorts?|chaussettes?|paires?|vetements?|hauts?|tops?|doudounes?|manteaux?|sacs?"
)
# Dans l'ordre de fiabilité : « lot de 5 », « pack de 3 », « x3 », « 3x », « 5 pulls ».
_MOTIFS_QUANTITE = [
    re.compile(r"\b(?:lot|pack|bundle)s? (?:de |d )?(\d{1,3})\b"),
    re.compile(r"\bx(\d{1,3})\b"),
    re.compile(r"\b(\d{1,3})x\b"),
    re.compile(rf"\b(\d{{1,3}}) (?:{_PIECES})\b"),
]
_MOT_LOT = re.compile(r"\b(?:lots?|bundle|pack|assortiment)\b")


@dataclass(frozen=True)
class Lot:
    quantite: int | None  # None : c'est un lot mais le nombre d'articles est inconnu ou incohérent


def detecter_lot(titre: str, quantite_max: int) -> Lot | None:
    """« Lot de 5 pulls » → Lot(5) ; « Lot de vêtements » → Lot(None) ; « Pull Nike » → None."""
    t = normaliser(titre)
    for motif in _MOTIFS_QUANTITE:
        m = motif.search(t)
        if m and 2 <= int(m.group(1)) <= quantite_max:
            return Lot(int(m.group(1)))
    return Lot(None) if _MOT_LOT.search(t) else None


def vente_pressee(texte: str, mots: list[str]) -> str | None:
    """Premier mot de vente pressée (« urgent », « déménagement »…) présent dans le texte, sinon None."""
    return premier_mot_present(normaliser(texte), mots)


def annonce_mal_valorisee(annonce: Annonce, reglages: Signaux, marques: CatalogueMarques) -> list[str]:
    """Raisons pour lesquelles l'annonce est peu mise en valeur (liste vide = bien présentée).

    - titre vague : peu de mots (ou uniquement des mots génériques), sans marque ni chiffre/modèle ;
    - une ou deux photos seulement (connu après le chargement du détail).
    """
    raisons: list[str] = []
    jetons = normaliser(annonce.titre).split()
    if jetons:
        marque_champ = set(normaliser(annonce.marque).split())
        marque_presente = marques.marque_dans_texte(annonce.titre) or any(j in marque_champ for j in jetons)
        a_un_chiffre = any(any(c.isdigit() for c in j) for j in jetons)
        generiques = {normaliser(g) for g in reglages.titres_generiques}
        trop_court = len(jetons) <= reglages.titre_vague_mots_max
        que_du_generique = all(j in generiques for j in jetons)
        if not marque_presente and not a_un_chiffre and (trop_court or que_du_generique):
            raisons.append("titre vague")
    if annonce.nb_photos is not None and annonce.nb_photos <= reglages.photos_max:
        raisons.append(f"{annonce.nb_photos} photo{'s' if annonce.nb_photos > 1 else ''}")
    return raisons
