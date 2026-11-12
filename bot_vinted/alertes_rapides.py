"""Alertes « rapides » créées depuis Telegram : chaque nouvelle annonce d'une recherche est signalée, sans calcul.

Une alerte est soit un lien de recherche Vinted collé tel quel, soit quelques mots-clés. Elle est stockée en base
(table `alertes_rapides`) et transformée en `Veille` en mode « toutes » par le moteur, qui prend en charge la
lecture de Vinted, le dédoublonnage, la pause en cas de blocage et l'envoi : aucune règle n'est contournée.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from bot_vinted.config import Config, Veille
from bot_vinted.vinted.url_recherche import UrlRechercheInvalide, analyser_url

PREFIXE_NOM = "Alerte"
LONGUEUR_MAX_MOTS = 80
_LIEN = re.compile(r"https?://\S+", re.IGNORECASE)


class SaisieInvalide(ValueError):
    """Le texte envoyé par l'utilisateur n'est pas une recherche exploitable (message affichable tel quel)."""


@dataclass(frozen=True)
class Saisie:
    url: str | None = None
    mots: str | None = None


def analyser_saisie(texte: str) -> Saisie:
    """Lien de recherche Vinted, ou mots-clés. Lève SaisieInvalide avec un message clair sinon."""
    texte = (texte or "").strip()
    if not texte:
        raise SaisieInvalide("Envoyez un lien de recherche Vinted ou quelques mots-clés, par exemple : "
                             "/ajouter pull ralph lauren")
    lien = _LIEN.search(texte)
    if lien:
        url = lien.group(0).rstrip(").,;")
        try:
            analyser_url(url)
        except UrlRechercheInvalide as e:
            raise SaisieInvalide(f"Ce lien n'est pas une recherche Vinted valide : {e}") from None
        return Saisie(url=url)
    mots = " ".join(texte.split())
    if len(mots) > LONGUEUR_MAX_MOTS:
        raise SaisieInvalide(f"Texte trop long ({LONGUEUR_MAX_MOTS} caractères maximum).")
    return Saisie(mots=mots)


def nom_veille(ligne: sqlite3.Row) -> str:
    detail = ligne["mots"] or "lien Vinted"
    return f"{PREFIXE_NOM} {ligne['id']} · {detail}"


def veille_depuis_ligne(ligne: sqlite3.Row, cfg: Config) -> Veille:
    """Veille en mode « toutes » correspondant à une alerte rapide."""
    return Veille(
        nom=nom_veille(ligne),
        url_recherche=ligne["url"],
        mots_cles=[ligne["mots"]] if ligne["mots"] else [],
        mode="toutes",
        marque_stricte=False,  # on veut TOUTES les annonces de la recherche, pas seulement celles d'une marque
        intervalle_secondes=cfg.telegram.intervalle_alertes_secondes,
        port_estime=0.0,
        marge_min=0.0,
        score_min=0,
    )
