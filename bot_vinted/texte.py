"""Utilitaires de normalisation de texte (recherche de mots, comparaison de marques…).

Tout le projet compare des textes « normalisés » : minuscules, sans accents, ponctuation
remplacée par des espaces simples. Ainsi « Abîmé ! » et « abime » sont équivalents.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


def normaliser(texte: str | None) -> str:
    """Minuscules, sans accents, uniquement [a-z0-9] séparés par un espace."""
    if not texte:
        return ""
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


@lru_cache(maxsize=2048)
def _motif_mot(mot_norm: str, tolerer_pluriel: bool) -> re.Pattern[str]:
    suffixe = "(?:e|s|es)?" if tolerer_pluriel else ""
    return re.compile(rf"(?<![a-z0-9]){re.escape(mot_norm)}{suffixe}(?![a-z0-9])")


def contient_mot(texte_norm: str, mot: str, tolerer_pluriel: bool = True) -> bool:
    """Vrai si `mot` (ou expression) apparaît comme MOT ENTIER dans `texte_norm`.

    Correspondance par mot entier : « trou » ne déclenche pas sur « trouvé ».
    `tolerer_pluriel` accepte les terminaisons e / s / es (« tache » → « taches », « abîmé » → « abîmée »).
    `texte_norm` doit déjà être passé dans `normaliser()`.
    """
    mot_norm = normaliser(mot)
    if not mot_norm or not texte_norm:
        return False
    return _motif_mot(mot_norm, tolerer_pluriel).search(texte_norm) is not None


def premier_mot_present(texte_norm: str, mots: list[str], tolerer_pluriel: bool = True) -> str | None:
    """Retourne le premier mot de `mots` présent dans `texte_norm`, sinon None."""
    for mot in mots:
        if contient_mot(texte_norm, mot, tolerer_pluriel):
            return mot
    return None
