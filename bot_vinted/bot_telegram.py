"""Commandes Telegram : gérer ses alertes depuis la conversation, sans toucher à `config.yaml`.

    /ajouter <lien de recherche Vinted | mots-clés>   crée une alerte « chaque nouvelle annonce »
    (un lien Vinted envoyé seul fonctionne aussi)     /liste · /supprimer <n> · /pause <n> · /reprendre <n> · /aide

Seul le chat `TELEGRAM_CHAT_ID` est écouté : toute autre conversation est ignorée. Le bot ne fait que lire les
messages envoyés à SON bot (API officielle de Telegram) ; il ne se connecte à aucun compte Vinted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bot_vinted.alertes.base import ErreurEnvoi
from bot_vinted.alertes.telegram import CanalTelegram
from bot_vinted.alertes_rapides import SaisieInvalide, analyser_saisie
from bot_vinted.base import Base
from bot_vinted.config import Config

log = logging.getLogger(__name__)

CLE_DECALAGE = "telegram_decalage"
ATTENTE_ERREUR = 10.0
AIDE = (
    "Je surveille Vinted et je vous préviens à chaque nouvelle annonce.\n\n"
    "/ajouter <lien Vinted ou mots-clés> : créer une alerte (vous pouvez aussi coller directement un lien de recherche)\n"
    "/liste : voir vos alertes\n"
    "/supprimer <n> : supprimer l'alerte n\n"
    "/pause <n> et /reprendre <n> : suspendre / relancer l'alerte n\n\n"
    "Astuce : sur Vinted, réglez vos filtres (marque, taille, prix…), copiez le lien de la page et collez-le ici."
)


class BotTelegram:
    def __init__(self, cfg: Config, base: Base, canal: CanalTelegram, *, horloge: Callable[[], float],
                 dormir: Callable[[float], Awaitable[bool]] | None = None) -> None:
        self.cfg, self.base, self.canal, self.horloge = cfg, base, canal, horloge
        self._dormir = dormir

    # ------------------------------------------------------------------ boucle

    async def executer(self, arret: asyncio.Event, attente: int = 25) -> None:
        """Lit les messages jusqu'à l'arrêt. Aucune erreur ne doit arrêter le bot : on attend et on réessaie."""
        log.info("Commandes Telegram actives (/ajouter, /liste, /supprimer…).")
        while not arret.is_set():
            try:
                await self.traiter_une_fois(attente)
            except ErreurEnvoi as e:
                log.warning("Commandes Telegram : %s. Nouvel essai dans %.0f s.", e, ATTENTE_ERREUR)
                await self._pause(arret, ATTENTE_ERREUR)
            except Exception:  # un message inattendu ne doit jamais couper la surveillance
                log.exception("Commandes Telegram : erreur inattendue.")
                await self._pause(arret, ATTENTE_ERREUR)

    async def _pause(self, arret: asyncio.Event, secondes: float) -> None:
        if self._dormir is not None:
            await self._dormir(secondes)
            return
        try:
            await asyncio.wait_for(arret.wait(), timeout=secondes)
        except asyncio.TimeoutError:
            pass

    async def traiter_une_fois(self, attente: int = 25) -> int:
        """Lit et traite les messages en attente ; retourne leur nombre."""
        brut = self.base.lire_meta(CLE_DECALAGE)
        decalage = int(brut) if brut and brut.lstrip("-").isdigit() else None
        mises_a_jour = await self.canal.recevoir(decalage, attente)
        for u in mises_a_jour:
            self.base.ecrire_meta(CLE_DECALAGE, str(u["update_id"] + 1))  # avant traitement : jamais deux fois
            try:
                await self._traiter(u)
            except ErreurEnvoi as e:
                log.warning("Commandes Telegram : réponse non envoyée (%s).", e)
        return len(mises_a_jour)

    # ------------------------------------------------------------------ commandes

    async def _traiter(self, maj: dict[str, Any]) -> None:
        message = maj.get("message") or {}
        conversation = (message.get("chat") or {}).get("id")
        texte = (message.get("text") or "").strip()
        if not texte or str(conversation) != (Config.secret(self.cfg.telegram.chat_id_env) or ""):
            if texte:
                log.warning("Message Telegram ignoré : conversation non autorisée.")
            return
        await self.canal.envoyer_texte(self.repondre(texte))

    def repondre(self, texte: str) -> str:
        """Texte de réponse à un message de l'utilisateur (applique la commande au passage)."""
        commande, _, reste = texte.partition(" ")
        commande = commande.split("@")[0].lower()  # « /liste@MonBot » → « /liste »
        reste = reste.strip()
        if commande in ("/start", "/aide", "/help"):
            return AIDE
        if commande == "/liste":
            return self._liste()
        if commande == "/ajouter":
            return self._ajouter(reste)
        if commande == "/supprimer":
            return self._sur_alerte(reste, "supprimer")
        if commande == "/pause":
            return self._sur_alerte(reste, "pause")
        if commande == "/reprendre":
            return self._sur_alerte(reste, "reprendre")
        if commande.startswith("/"):
            return "Commande inconnue. Tapez /aide pour la liste des commandes."
        if "http" in texte.lower():  # lien collé seul = nouvelle alerte
            return self._ajouter(texte)
        return "Pour créer une alerte, envoyez un lien de recherche Vinted ou /ajouter suivi de mots-clés. /aide pour le détail."

    def _ajouter(self, texte: str) -> str:
        if len(self.base.alertes_rapides()) >= self.cfg.telegram.alertes_max:
            return f"Limite atteinte ({self.cfg.telegram.alertes_max} alertes). Supprimez-en une avec /supprimer <n>."
        try:
            saisie = analyser_saisie(texte)
        except SaisieInvalide as e:
            return str(e)
        identifiant = self.base.ajouter_alerte_rapide(url=saisie.url, mots=saisie.mots, maintenant=self.horloge())
        quoi = "ce lien de recherche" if saisie.url else f"« {saisie.mots} »"
        return (f"✅ Alerte {identifiant} créée pour {quoi}.\n"
                f"Les annonces déjà en ligne sont ignorées : vous serez prévenu des NOUVELLES, vérification "
                f"toutes les {self.cfg.telegram.intervalle_alertes_secondes} s environ.")

    def _liste(self) -> str:
        lignes = self.base.alertes_rapides()
        if not lignes:
            return "Aucune alerte. Envoyez un lien de recherche Vinted ou /ajouter <mots-clés>."
        return "Vos alertes :\n" + "\n".join(
            f"{'▶️' if r['actif'] else '⏸️'} {r['id']} · {r['mots'] or r['url']}" for r in lignes)

    def _sur_alerte(self, reste: str, action: str) -> str:
        if not reste.isdigit():
            return f"Indiquez le numéro de l'alerte, par exemple : /{'supprimer' if action == 'supprimer' else action} 3 (voir /liste)."
        identifiant = int(reste)
        if action == "supprimer":
            ok = self.base.supprimer_alerte_rapide(identifiant)
        else:
            ok = self.base.activer_alerte_rapide(identifiant, action == "reprendre")
        if not ok:
            return f"Alerte {identifiant} introuvable (voir /liste)."
        return {"supprimer": f"🗑️ Alerte {identifiant} supprimée.", "pause": f"⏸️ Alerte {identifiant} en pause.",
                "reprendre": f"▶️ Alerte {identifiant} relancée."}[action]
