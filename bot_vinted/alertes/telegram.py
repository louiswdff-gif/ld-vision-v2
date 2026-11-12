"""Alertes Telegram (optionnel) : même contenu que Discord, en photo + légende HTML.

Le token du bot est un SECRET (lu dans .env). Il figure dans l'URL de l'API Telegram : aucune URL n'est
donc jamais écrite dans un message d'erreur, et le journal masque les motifs `/bot<id>:<token>`.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from bot_vinted.alertes.base import (
    Alerte,
    ErreurEnvoi,
    euros,
    libelle_etat,
    libelle_revente,
    libelle_vendeur,
    pourcentage,
    temps_ecoule,
)
from bot_vinted.config import Config

log = logging.getLogger(__name__)

LIMITE_LEGENDE = 1024  # une légende de photo est limitée à 1024 caractères
LIMITE_MESSAGE = 4096
MAX_TENTATIVES = 3
ATTENTE_MAX_429 = 30.0


def _e(texte: object) -> str:
    return html.escape(str(texte), quote=False)


class CanalTelegram:
    nom = "telegram"

    def __init__(
        self,
        cfg: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._dormir = dormir
        self._http = httpx.AsyncClient(timeout=cfg.telegram.timeout_secondes, transport=transport)

    async def fermer(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ mise en forme

    @staticmethod
    def construire_texte(a: Alerte, maintenant: float, limite: int = LIMITE_MESSAGE) -> str:
        """Texte HTML de l'alerte, raccourci (signaux d'abord) pour ne pas dépasser `limite` caractères."""
        if a.simple:
            return "\n".join([
                f"{a.palier} <b>{_e(a.titre)}</b>",
                f"{_e(a.marque or '—')} · {_e(a.taille or '—')} · {_e(libelle_etat(a.etat))}",
                f"💶 <b>{_e(euros(a.prix))}</b> · ⏱ {_e(temps_ecoule(a.publie_le, maintenant))}",
                f"🔎 {_e(a.veille)}",
                f'<a href="{html.escape(a.url, quote=True)}">Voir l\'annonce sur Vinted</a>',
            ])[:limite]
        tete = [
            f"{a.palier} <b>{_e(a.titre)}</b>",
            f"{_e(a.marque or '—')} · {_e(a.taille or '—')} · {_e(libelle_etat(a.etat))}",
            f"💶 Prix {_e(euros(a.prix))} · coût réel {_e(euros(a.cout_reel))}",
            f"🔁 Revente estimée {_e(libelle_revente(a))}",
            f"📈 Marge <b>{_e(euros(a.marge, signe=True))}</b> · ROI {_e(pourcentage(a.roi_pct))} · "
            f"score <b>{a.score}/100</b>",
        ]
        queue = [
            f"👤 {_e(libelle_vendeur(a.vendeur_note, a.vendeur_avis))} · ⏱ {_e(temps_ecoule(a.publie_le, maintenant))}",
            f'<a href="{html.escape(a.url, quote=True)}">Voir l\'annonce sur Vinted</a>',
        ]
        tags = [_e(t) for t in a.tags]
        while True:
            texte = "\n".join(tete + tags + queue)
            if len(texte) <= limite or not tags:
                return texte
            tags.pop()  # trop long : on retire d'abord le dernier signal

    # ------------------------------------------------------------------ envoi

    def _identifiants(self) -> tuple[str, str]:
        token = Config.secret(self._cfg.telegram.token_env)
        chat = Config.secret(self._cfg.telegram.chat_id_env)
        if not token or not chat:
            raise ErreurEnvoi(
                f"variables {self._cfg.telegram.token_env} / {self._cfg.telegram.chat_id_env} absentes (.env)"
            )
        return token, chat

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> None:
        token, chat = self._identifiants()
        bouton = {"inline_keyboard": [[{"text": "Voir l'annonce", "url": alerte.url}]]}
        if alerte.photo_url:
            charge = {
                "chat_id": chat,
                "photo": alerte.photo_url,
                "caption": self.construire_texte(alerte, maintenant, LIMITE_LEGENDE),
                "parse_mode": "HTML",
                "reply_markup": bouton,
            }
            try:
                await self._appel(token, "sendPhoto", charge)
                return
            except ErreurEnvoi as e:
                # Photo inaccessible pour Telegram : on envoie le texte seul plutôt que de perdre l'alerte.
                log.info("Telegram : photo refusée (%s), envoi du texte seul.", e)
        charge = {
            "chat_id": chat,
            "text": self.construire_texte(alerte, maintenant),
            "parse_mode": "HTML",
            "reply_markup": bouton,
            "disable_web_page_preview": False,
        }
        await self._appel(token, "sendMessage", charge)

    async def envoyer_texte(self, message: str) -> None:
        token, chat = self._identifiants()
        await self._appel(token, "sendMessage", {"chat_id": chat, "text": message[:LIMITE_MESSAGE]})

    async def recevoir(self, decalage: int | None, attente: int = 25) -> list[dict[str, Any]]:
        """Messages reçus par le bot (interrogation longue). Lève ErreurEnvoi en cas d'échec (jamais le token)."""
        token, _ = self._identifiants()
        charge: dict[str, Any] = {"timeout": attente, "allowed_updates": ["message"]}
        if decalage is not None:
            charge["offset"] = decalage
        try:
            reponse = await self._http.post(f"https://api.telegram.org/bot{token}/getUpdates", json=charge,
                                            timeout=attente + 15)
        except httpx.TransportError as e:
            raise ErreurEnvoi(f"erreur réseau ({type(e).__name__})") from None
        if reponse.status_code == 409:
            raise ErreurEnvoi("un autre programme lit déjà les messages de ce bot (HTTP 409)")
        if reponse.status_code != 200:
            raise ErreurEnvoi(f"Telegram a répondu HTTP {reponse.status_code}")
        try:
            return list(reponse.json().get("result", []))
        except (ValueError, AttributeError):
            raise ErreurEnvoi("réponse Telegram illisible") from None

    async def _appel(self, token: str, methode: str, charge: dict[str, Any]) -> None:
        url = f"https://api.telegram.org/bot{token}/{methode}"
        derniere_erreur = "inconnue"
        for tentative in range(1, MAX_TENTATIVES + 1):
            try:
                reponse = await self._http.post(url, json=charge)
            except httpx.TransportError as e:
                derniere_erreur = f"erreur réseau ({type(e).__name__})"
            else:
                code = reponse.status_code
                if code == 200:
                    return
                if code == 429:
                    attente = min(_retry_after(reponse), ATTENTE_MAX_429)
                    derniere_erreur = f"limitation Telegram (HTTP 429, attente {attente:g} s)"
                    if tentative < MAX_TENTATIVES:
                        await self._dormir(attente)
                    continue
                if code >= 500:
                    derniere_erreur = f"erreur serveur Telegram (HTTP {code})"
                else:  # 400/401/403/404 : token ou chat invalide, contenu refusé…
                    raise ErreurEnvoi(f"Telegram a refusé {methode} (HTTP {code}) : {_description(reponse)}")
            if tentative < MAX_TENTATIVES:
                await self._dormir(2.0 * tentative)
        raise ErreurEnvoi(f"échec après {MAX_TENTATIVES} tentatives : {derniere_erreur}")


def _description(reponse: httpx.Response) -> str:
    try:
        return str(reponse.json().get("description", "sans détail"))[:200]
    except (ValueError, AttributeError):
        return "sans détail"


def _retry_after(reponse: httpx.Response) -> float:
    try:
        return max(0.5, float(reponse.json()["parameters"]["retry_after"]))
    except (ValueError, KeyError, TypeError):
        return 2.0


async def decouvrir_conversations(token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> list[tuple[int, str]]:
    """Conversations ayant écrit au bot : [(identifiant, libellé)]. Sert à trouver TELEGRAM_CHAT_ID.

    Lève ErreurEnvoi si le token est refusé ou si Telegram est injoignable (sans jamais citer le token)."""
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            reponse = await http.get(f"https://api.telegram.org/bot{token}/getUpdates")
    except httpx.TransportError as e:
        raise ErreurEnvoi(f"Telegram injoignable ({type(e).__name__})") from None
    if reponse.status_code in (401, 404):
        raise ErreurEnvoi("Telegram refuse ce token : vérifiez TELEGRAM_BOT_TOKEN dans .env")
    if reponse.status_code != 200:
        raise ErreurEnvoi(f"Telegram a répondu HTTP {reponse.status_code}")
    try:
        mises_a_jour = reponse.json().get("result", [])
    except (ValueError, AttributeError):
        raise ErreurEnvoi("réponse Telegram illisible") from None
    trouvees: dict[int, str] = {}
    for u in mises_a_jour:
        conv = (u.get("message") or u.get("edited_message") or u.get("channel_post") or {}).get("chat")
        if isinstance(conv, dict) and isinstance(conv.get("id"), int):
            nom = conv.get("title") or " ".join(filter(None, [conv.get("first_name"), conv.get("last_name")])) \
                or conv.get("username") or "?"
            trouvees[conv["id"]] = f"{nom} ({conv.get('type', '?')})"
    return list(trouvees.items())
