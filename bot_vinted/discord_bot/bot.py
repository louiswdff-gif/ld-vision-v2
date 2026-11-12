"""Couche discord.py du bot interactif.

Ce que fait le bot dans Discord :
- il POSTE chaque alerte dans le salon de la veille (embed + boutons) à la place du webhook ;
- boutons de chaque alerte : « Voir sur Vinted » (simple LIEN : l'achat reste manuel), « J'ai acheté »
  (formulaire → enregistre l'achat), puis « Vendu » (formulaire → enregistre la revente) ;
- commandes : /top, /veilles, /etat (ouvertes à tous les membres du serveur) et /stats, /acheter, /vendu
  (réservées aux `utilisateurs_autorises`).

Les boutons sont « persistants » (DynamicItem) : ils continuent de fonctionner après un redémarrage du bot.
Aucun achat, message ou offre n'est jamais envoyé à Vinted ou à un vendeur.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Literal

import discord
from discord import app_commands

from bot_vinted.alertes.base import Alerte, ErreurEnvoi
from bot_vinted.alertes.discord import CanalDiscord
from bot_vinted.base import Base
from bot_vinted.config import Config, ErreurConfig
from bot_vinted.discord_bot.logique import (
    analyser_prix,
    autorise,
    embed_etat,
    embed_stats,
    embed_top,
    embed_veilles,
    enregistrer_achat,
    enregistrer_vente,
    id_bouton,
)

log = logging.getLogger(__name__)

AUCUNE_MENTION = discord.AllowedMentions.none()  # une annonce ne doit jamais pouvoir pinger @everyone
REFUS_ECRITURE = (
    "⛔ Vous n'êtes pas autorisé à enregistrer des achats ou des ventes.\n"
    "Ajoutez votre identifiant Discord dans `discord_bot.utilisateurs_autorises` (config.yaml)."
)
ERREUR_INTERNE = "❌ Une erreur est survenue (détails dans les logs du bot)."
EtatVue = Literal["a_acheter", "achete", "vendu"]


def vers_embed(contenu: dict) -> discord.Embed:
    return discord.Embed.from_dict(contenu)


def url_sure(url: str | None, domaine: str, annonce_id: int) -> str:
    """URL du bouton « Voir sur Vinted » : https obligatoire, sinon lien canonique reconstruit."""
    if url and url.startswith("https://") and len(url) <= 512 and " " not in url:
        return url
    return f"https://www.{domaine}/items/{annonce_id}"


def construire_vue(url: str | None, domaine: str, annonce_id: int, etat: EtatVue = "a_acheter") -> discord.ui.View:
    """Boutons d'une alerte selon son avancement : à acheter → acheté (à revendre) → vendu."""
    vue = discord.ui.View(timeout=None)
    vue.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Voir sur Vinted", emoji="🔗",
                                   url=url_sure(url, domaine, annonce_id)))
    if etat == "a_acheter":
        vue.add_item(BoutonAchete(domaine, annonce_id))
    elif etat == "achete":
        vue.add_item(BoutonVendu(domaine, annonce_id))
    else:
        vue.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="Vendu", emoji="✅", disabled=True,
                                       custom_id="vt:termine"))
    return vue


# --------------------------------------------------------------------------- formulaire prix


class ModalPrix(discord.ui.Modal):
    """Formulaire de saisie d'un prix : total payé (achat) ou montant net reçu (vente)."""

    def __init__(self, mode: Literal["achat", "vente"], domaine: str, annonce_id: int, defaut: str = "") -> None:
        super().__init__(title="J'ai acheté cet article" if mode == "achat" else "J'ai revendu cet article",
                         timeout=None)
        self.mode, self.domaine, self.annonce_id = mode, domaine, annonce_id
        self.prix = discord.ui.TextInput(
            label="Total payé, tout compris (€)" if mode == "achat" else "Montant net reçu (€)",
            placeholder="36,10", default=defaut or None, required=True, min_length=1, max_length=12,
        )
        self.add_item(self.prix)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: BotDiscord = interaction.client  # type: ignore[assignment]
        if not autorise(bot.cfg, interaction.user.id):  # revérifié : la config a pu changer pendant la saisie
            await interaction.response.send_message(REFUS_ECRITURE, ephemeral=True)
            return
        try:
            prix = analyser_prix(self.prix.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        enregistrer = enregistrer_achat if self.mode == "achat" else enregistrer_vente
        ok, message = enregistrer(bot.base, bot.cfg, str(self.annonce_id), prix, bot.horloge(), domaine=self.domaine)
        texte = ("✅ " if ok else "❌ ") + message
        if ok and interaction.message is not None:
            # Le message d'alerte change de boutons : « J'ai acheté » devient « Vendu », puis « Vendu ✔ ».
            etat: EtatVue = "achete" if self.mode == "achat" else "vendu"
            vue = construire_vue(bot.url_annonce(self.domaine, self.annonce_id), self.domaine, self.annonce_id, etat)
            await interaction.response.edit_message(view=vue)
            await interaction.followup.send(texte, ephemeral=True)
        else:
            await interaction.response.send_message(texte, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Erreur dans le formulaire de prix : %s", error, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(ERREUR_INTERNE, ephemeral=True)
            else:
                await interaction.response.send_message(ERREUR_INTERNE, ephemeral=True)


# --------------------------------------------------------------------------- boutons persistants


class BoutonAchete(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"vt:achete:(?P<domaine>vinted\.[a-z.]+):(?P<id>\d+)"):
    def __init__(self, domaine: str, annonce_id: int) -> None:
        super().__init__(discord.ui.Button(label="J'ai acheté", emoji="🛒", style=discord.ButtonStyle.success,
                                           custom_id=id_bouton("achete", domaine, annonce_id)))
        self.domaine, self.annonce_id = domaine, annonce_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> "BoutonAchete":
        return cls(match["domaine"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: BotDiscord = interaction.client  # type: ignore[assignment]
        if not autorise(bot.cfg, interaction.user.id):
            await interaction.response.send_message(REFUS_ECRITURE, ephemeral=True)
            return
        ligne = bot.base.annonce(self.domaine, self.annonce_id)
        # Prix proposé par défaut : le coût réel estimé (article + protection + port), modifiable avant validation.
        defaut = f"{ligne['cout_reel']:.2f}".replace(".", ",") if ligne else ""
        await interaction.response.send_modal(ModalPrix("achat", self.domaine, self.annonce_id, defaut))


class BoutonVendu(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"vt:vendu:(?P<domaine>vinted\.[a-z.]+):(?P<id>\d+)"):
    def __init__(self, domaine: str, annonce_id: int) -> None:
        super().__init__(discord.ui.Button(label="Vendu", emoji="💰", style=discord.ButtonStyle.primary,
                                           custom_id=id_bouton("vendu", domaine, annonce_id)))
        self.domaine, self.annonce_id = domaine, annonce_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> "BoutonVendu":
        return cls(match["domaine"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: BotDiscord = interaction.client  # type: ignore[assignment]
        if not autorise(bot.cfg, interaction.user.id):
            await interaction.response.send_message(REFUS_ECRITURE, ephemeral=True)
            return
        await interaction.response.send_modal(ModalPrix("vente", self.domaine, self.annonce_id))


# --------------------------------------------------------------------------- le bot


class BotDiscord(discord.Client):
    def __init__(self, cfg: Config, base: Base, *, horloge: Callable[[], float] = time.time) -> None:
        super().__init__(intents=discord.Intents.default())  # aucun intent privilégié : pas de lecture des messages
        self.cfg, self.base, self.horloge = cfg, base, horloge
        self.tree = app_commands.CommandTree(self)
        self._pret = asyncio.Event()
        self._tache: asyncio.Task | None = None
        self._definir_commandes()

    # ----- cycle de vie

    async def setup_hook(self) -> None:
        self.add_dynamic_items(BoutonAchete, BoutonVendu)  # boutons des anciennes alertes toujours actifs
        await self._synchroniser_commandes()

    async def on_ready(self) -> None:
        log.info("Bot Discord connecté en tant que %s.", self.user)
        self._pret.set()

    async def _synchroniser_commandes(self) -> None:
        serveur_id = self.cfg.discord_bot.serveur_id
        try:
            if serveur_id:  # commandes de serveur : disponibles immédiatement
                serveur = discord.Object(id=serveur_id)
                self.tree.copy_global_to(guild=serveur)
                await self.tree.sync(guild=serveur)
            else:  # commandes globales : jusqu'à une heure de délai côté Discord
                await self.tree.sync()
            log.info("Commandes Discord synchronisées (%s).", "serveur" if serveur_id else "globales")
        except discord.HTTPException as e:
            log.warning("Synchronisation des commandes impossible (HTTP %s) : le bot doit être invité avec le "
                        "scope « applications.commands » (voir README).", e.status)

    async def demarrer(self) -> None:
        """Connecte le bot en tâche de fond et attend qu'il soit prêt. Lève ErreurConfig si le token est refusé."""
        token = Config.secret(self.cfg.discord_bot.token_env)
        if not token:
            raise ErreurConfig(f"Variable d'environnement {self.cfg.discord_bot.token_env} absente (.env) : "
                               "token du bot Discord requis.")
        self._tache = asyncio.create_task(self.start(token), name="bot-discord")
        attente = asyncio.create_task(self._pret.wait())
        fait, _ = await asyncio.wait({self._tache, attente}, timeout=self.cfg.discord_bot.timeout_connexion_secondes,
                                     return_when=asyncio.FIRST_COMPLETED)
        if self._tache in fait:  # la connexion a échoué avant d'être prête
            attente.cancel()
            erreur = self._tache.exception()
            if isinstance(erreur, discord.LoginFailure):
                raise ErreurConfig("Discord a refusé le token du bot : vérifiez DISCORD_BOT_TOKEN dans .env.") from None
            raise ErreurConfig(f"Connexion à Discord impossible : {type(erreur).__name__}") from None
        if attente not in fait:
            attente.cancel()
            log.warning("Bot Discord pas encore connecté après %.0f s : les alertes sont conservées et seront "
                        "envoyées dès la connexion.", self.cfg.discord_bot.timeout_connexion_secondes)
        self._tache.add_done_callback(self._journaliser_fin)

    @staticmethod
    def _journaliser_fin(tache: asyncio.Task) -> None:
        if not tache.cancelled() and tache.exception() is not None:
            log.error("Le bot Discord s'est arrêté : %s", type(tache.exception()).__name__)

    async def arreter(self) -> None:
        await self.close()
        if self._tache is not None:
            self._tache.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tache

    def url_annonce(self, domaine: str, annonce_id: int) -> str:
        ligne = self.base.annonce(domaine, annonce_id)
        return url_sure(ligne["url"] if ligne else None, domaine, annonce_id)

    # ----- commandes

    def _definir_commandes(self) -> None:
        tree = self.tree

        @tree.command(name="top", description="Meilleures affaires détectées récemment")
        @app_commands.describe(heures="Période en heures (1 à 168)", nombre="Nombre d'annonces (1 à 10)")
        @app_commands.guild_only()
        async def top(interaction: discord.Interaction, heures: app_commands.Range[int, 1, 168] = 24,
                      nombre: app_commands.Range[int, 1, 10] = 5) -> None:
            contenu = embed_top(self.base, self.cfg, self.horloge(), heures, nombre)
            await interaction.response.send_message(embed=vers_embed(contenu), ephemeral=True,
                                                    allowed_mentions=AUCUNE_MENTION)

        @tree.command(name="veilles", description="Veilles surveillées, réglages et activité récente")
        @app_commands.guild_only()
        async def veilles(interaction: discord.Interaction) -> None:
            contenu = embed_veilles(self.cfg, self.base, self.horloge())
            await interaction.response.send_message(embed=vers_embed(contenu), ephemeral=True,
                                                    allowed_mentions=AUCUNE_MENTION)

        @tree.command(name="etat", description="État du bot : actif, en pause (motif, reprise) ou silencieux")
        @app_commands.guild_only()
        async def etat(interaction: discord.Interaction) -> None:
            contenu = embed_etat(self.cfg, self.base, self.horloge())
            await interaction.response.send_message(embed=vers_embed(contenu), ephemeral=True,
                                                    allowed_mentions=AUCUNE_MENTION)

        @tree.command(name="stats", description="Résultats réels : marge, ROI, délai de revente, veilles rentables")
        @app_commands.guild_only()
        async def stats(interaction: discord.Interaction) -> None:
            if not autorise(self.cfg, interaction.user.id):
                await interaction.response.send_message(REFUS_ECRITURE, ephemeral=True)
                return
            await interaction.response.send_message(embed=vers_embed(embed_stats(self.base)), ephemeral=True,
                                                    allowed_mentions=AUCUNE_MENTION)

        @tree.command(name="acheter", description="Enregistre un article acheté (total payé, tout compris)")
        @app_commands.describe(annonce="Identifiant ou URL de l'annonce Vinted", prix="Total payé en €, ex. 36,10")
        @app_commands.guild_only()
        async def acheter(interaction: discord.Interaction, annonce: str, prix: str) -> None:
            await self._commande_prix(interaction, enregistrer_achat, annonce, prix)

        @tree.command(name="vendu", description="Enregistre la revente d'un article (montant net reçu)")
        @app_commands.describe(annonce="Identifiant ou URL de l'annonce Vinted", prix="Montant net reçu en €")
        @app_commands.guild_only()
        async def vendu(interaction: discord.Interaction, annonce: str, prix: str) -> None:
            await self._commande_prix(interaction, enregistrer_vente, annonce, prix)

        @tree.error
        async def erreur_commande(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            log.error("Erreur de commande Discord : %s", error, exc_info=error)
            with contextlib.suppress(discord.HTTPException):
                if interaction.response.is_done():
                    await interaction.followup.send(ERREUR_INTERNE, ephemeral=True)
                else:
                    await interaction.response.send_message(ERREUR_INTERNE, ephemeral=True)

    async def _commande_prix(self, interaction: discord.Interaction, enregistrer: Callable, annonce: str,
                             prix: str) -> None:
        if not autorise(self.cfg, interaction.user.id):
            await interaction.response.send_message(REFUS_ECRITURE, ephemeral=True)
            return
        try:
            montant = analyser_prix(prix)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        ok, message = enregistrer(self.base, self.cfg, annonce, montant, self.horloge())
        await interaction.response.send_message(("✅ " if ok else "❌ ") + message, ephemeral=True,
                                                allowed_mentions=AUCUNE_MENTION)


# --------------------------------------------------------------------------- canal d'alertes


class CanalBotDiscord:
    """Canal d'alertes (même interface que les webhooks) : poste dans le salon de la veille, avec les boutons."""

    nom = "discord-bot"

    def __init__(self, bot: BotDiscord) -> None:
        self.bot = bot

    async def _salon(self, salon_id: int):
        if not self.bot.is_ready():
            raise ErreurEnvoi("bot Discord pas encore connecté (l'alerte est conservée)")
        salon = self.bot.get_channel(salon_id)
        if salon is None:
            try:
                salon = await self.bot.fetch_channel(salon_id)
            except (discord.NotFound, discord.Forbidden):
                raise ErreurEnvoi(f"salon {salon_id} introuvable ou inaccessible : vérifiez l'ID et que le bot "
                                  "a accès à ce salon") from None
            except discord.HTTPException as e:
                raise ErreurEnvoi(f"Discord a répondu une erreur (HTTP {e.status}) pour le salon {salon_id}") from None
        if not hasattr(salon, "send"):
            raise ErreurEnvoi(f"{salon_id} n'est pas un salon textuel")
        return salon

    async def _envoyer(self, salon_id: int, **contenu) -> None:
        salon = await self._salon(salon_id)
        try:
            await salon.send(allowed_mentions=AUCUNE_MENTION, **contenu)
        except discord.Forbidden:
            raise ErreurEnvoi("permission refusée dans le salon : le bot doit pouvoir le voir, y écrire et y "
                              "poster des intégrations (embeds)") from None
        except discord.HTTPException as e:
            raise ErreurEnvoi(f"Discord a refusé le message (HTTP {e.status})") from None

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> None:
        salon_id = alerte.salon_id or self.bot.cfg.discord_bot.salon_id
        embed = vers_embed(CanalDiscord.construire_embed(alerte, maintenant))
        vue = construire_vue(alerte.url, alerte.domaine, alerte.annonce_id)
        await self._envoyer(salon_id, embed=embed, view=vue)

    async def envoyer_texte(self, message: str) -> None:
        await self._envoyer(self.bot.cfg.discord_bot.salon_id, content=message[:2000])

    async def fermer(self) -> None:
        return None  # la connexion est fermée par la commande `run` (BotDiscord.arreter)
