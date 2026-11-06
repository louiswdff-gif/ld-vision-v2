"""Canal console : utilisé en `--dry-run`. Affiche l'alerte telle qu'elle serait envoyée, n'envoie RIEN."""

from __future__ import annotations

from collections.abc import Callable

from bot_vinted.alertes.base import (
    Alerte,
    euros,
    libelle_etat,
    libelle_revente,
    libelle_vendeur,
    pourcentage,
    temps_ecoule,
)


class CanalConsole:
    nom = "console"

    def __init__(self, sortie: Callable[[str], None] = print) -> None:
        self._sortie = sortie

    @staticmethod
    def formater(a: Alerte, maintenant: float) -> str:
        lignes = [
            "─" * 64,
            f"{a.palier} [DRY-RUN] {a.titre}",
            f"   Veille   : {a.veille}  ({a.domaine})",
            f"   Marque   : {a.marque or '—'} · Taille : {a.taille or '—'} · État : {libelle_etat(a.etat)}",
            f"   Prix     : {euros(a.prix)} · Coût réel : {euros(a.cout_reel)} · Revente estimée : {libelle_revente(a)}",
            f"   Marge    : {euros(a.marge, signe=True)} · ROI : {pourcentage(a.roi_pct)} · Deal score : {a.score}/100",
        ]
        lignes += [f"   Tag      : {t}" for t in a.tags]
        lignes += [
            f"   Vendeur  : {libelle_vendeur(a.vendeur_note, a.vendeur_avis)}",
            f"   Publiée  : {temps_ecoule(a.publie_le, maintenant)}",
            f"   Lien     : {a.url}",
            "─" * 64,
        ]
        return "\n".join(lignes)

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> None:
        self._sortie(self.formater(alerte, maintenant))

    async def envoyer_texte(self, message: str) -> None:
        self._sortie(f"[DRY-RUN] Message système : {message}")

    async def fermer(self) -> None:
        return None
