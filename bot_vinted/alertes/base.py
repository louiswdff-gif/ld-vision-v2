"""Contenu commun des alertes et diffusion vers les canaux.

Une `Alerte` contient tout ce qu'il faut afficher ; chaque canal (Discord, Telegram, console) la met en forme.
Le bot ne fait QUE détecter : l'alerte donne le lien, c'est l'utilisateur qui achète.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

LIBELLES_ETATS = {
    "neuf_avec_etiquette": "Neuf avec étiquette",
    "neuf_sans_etiquette": "Neuf sans étiquette",
    "tres_bon_etat": "Très bon état",
    "bon_etat": "Bon état",
    "satisfaisant": "Satisfaisant",
}
SEPARATEUR_TAGS = "\n"


class ErreurEnvoi(Exception):
    """L'envoi d'une alerte a échoué (webhook invalide, réseau, limitation…)."""


@dataclass
class Alerte:
    domaine: str
    annonce_id: int
    veille: str
    titre: str
    url: str
    prix: float
    cout_reel: float
    revente_estimee: float
    source_revente: str  # « mediane » | « cible »
    nb_references: int
    marge: float
    roi_pct: float
    score: int
    palier: str  # « 🔥 » | « ✅ » | « 🔹 »
    marque: str = ""
    taille: str = ""
    etat: str | None = None
    photo_url: str | None = None
    tags: list[str] = field(default_factory=list)
    publie_le: float | None = None
    vendeur_note: float | None = None
    vendeur_avis: int | None = None
    webhook: str | None = None  # NOM de la variable d'environnement du webhook Discord de la veille
    salon_id: int | None = None  # ID du salon Discord de la veille (bot Discord interactif)

    @classmethod
    def depuis_ligne(cls, ligne: sqlite3.Row, palier: str, webhook: str | None,
                     salon_id: int | None = None) -> "Alerte":
        """Reconstruit une alerte depuis la table `annonces` (renvoi d'une alerte restée en attente)."""
        return cls(
            domaine=ligne["domaine"], annonce_id=ligne["annonce_id"], veille=ligne["veille"], titre=ligne["titre"],
            url=ligne["url"], prix=ligne["prix"], cout_reel=ligne["cout_reel"], revente_estimee=ligne["revente_estimee"],
            source_revente=ligne["source_revente"], nb_references=ligne["nb_references"], marge=ligne["marge"],
            roi_pct=ligne["roi"], score=ligne["score"], palier=palier, marque=ligne["marque"], taille=ligne["taille"],
            etat=ligne["etat"], photo_url=ligne["photo_url"],
            tags=[t for t in (ligne["tags"] or "").split(SEPARATEUR_TAGS) if t],
            publie_le=ligne["publie_le"], vendeur_note=ligne["vendeur_note"], vendeur_avis=ligne["vendeur_avis"],
            webhook=webhook, salon_id=salon_id,
        )


# --------------------------------------------------------------------------- mise en forme commune


def euros(montant: float, signe: bool = False) -> str:
    """28.0 → « 28,00 € » ; avec signe=True : « +33,40 € » / « -5,00 € »."""
    texte = f"{abs(montant):,.2f}".replace(",", " ").replace(".", ",")
    prefixe = ("-" if montant < 0 else "+") if signe else ("-" if montant < 0 else "")
    return f"{prefixe}{texte} €"


def temps_ecoule(publie_le: float | None, maintenant: float) -> str:
    """« il y a 3 min », « il y a 2 h 05 », « il y a 3 j » ; « date inconnue » si la publication est inconnue."""
    if publie_le is None:
        return "date inconnue"
    secondes = max(0, int(maintenant - publie_le))
    if secondes < 60:
        return "à l'instant"
    minutes = secondes // 60
    if minutes < 60:
        return f"il y a {minutes} min"
    heures, reste = divmod(minutes, 60)
    if heures < 24:
        return f"il y a {heures} h {reste:02d}"
    return f"il y a {heures // 24} j"


def libelle_etat(etat: str | None) -> str:
    return LIBELLES_ETATS.get(etat or "", "Non précisé")


def libelle_vendeur(note: float | None, avis: int | None) -> str:
    if avis == 0:
        return "Aucun avis"
    if note is None and avis is None:
        return "Inconnu"
    morceaux = []
    if note is not None:
        morceaux.append(f"{note:.1f} ★".replace(".", ","))
    if avis is not None:
        morceaux.append(f"({avis} avis)")
    return " ".join(morceaux)


def libelle_revente(a: Alerte) -> str:
    if a.source_revente == "mediane":
        return f"{euros(a.revente_estimee)} (médiane de {a.nb_references} annonces)"
    if a.source_revente == "mediane_toutes_tailles":
        return f"{euros(a.revente_estimee)} (médiane de {a.nb_references} annonces, toutes tailles)"
    return f"{euros(a.revente_estimee)} (prix cible de la veille)"


def pourcentage(x: float) -> str:
    return f"{x:+.0f} %"


# --------------------------------------------------------------------------- canaux


class Canal(Protocol):
    nom: str

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> None:
        """Envoie une alerte ; lève ErreurEnvoi en cas d'échec."""

    async def envoyer_texte(self, message: str) -> None:
        """Envoie un message système (pause / reprise du bot) ; lève ErreurEnvoi en cas d'échec."""

    async def fermer(self) -> None: ...


class Diffuseur:
    """Envoie à tous les canaux configurés. Le PREMIER canal est la référence : l'alerte est considérée comme
    envoyée dès qu'au moins un canal a réussi (évite les doublons Discord si seul Telegram est en panne)."""

    def __init__(self, canaux: list[Canal]) -> None:
        self.canaux = canaux

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> bool:
        reussi = False
        for canal in self.canaux:
            try:
                await canal.envoyer_alerte(alerte, maintenant)
                reussi = True
            except ErreurEnvoi as e:
                log.warning("Alerte %s : échec d'envoi sur %s (%s).", alerte.annonce_id, canal.nom, e)
        return reussi

    async def envoyer_texte(self, message: str) -> bool:
        reussi = False
        for canal in self.canaux:
            try:
                await canal.envoyer_texte(message)
                reussi = True
            except ErreurEnvoi as e:
                log.warning("Message système : échec d'envoi sur %s (%s).", canal.nom, e)
        return reussi

    async def fermer(self) -> None:
        for canal in self.canaux:
            await canal.fermer()


def construire_diffuseur(cfg, dry_run: bool, bot=None) -> Diffuseur:
    """Console seule en --dry-run (rien n'est envoyé) ; sinon le bot Discord interactif s'il est fourni, ou les
    webhooks Discord ; Telegram en plus si activé."""
    from bot_vinted.alertes.console import CanalConsole
    from bot_vinted.alertes.discord import CanalDiscord
    from bot_vinted.alertes.telegram import CanalTelegram

    if dry_run:
        return Diffuseur([CanalConsole()])
    if bot is not None:
        from bot_vinted.discord_bot.bot import CanalBotDiscord

        canaux: list[Canal] = [CanalBotDiscord(bot)]
    else:
        canaux = [CanalDiscord(cfg)]
    if cfg.telegram.actif:
        canaux.append(CanalTelegram(cfg))
    return Diffuseur(canaux)
