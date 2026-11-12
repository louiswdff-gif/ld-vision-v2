"""Alertes Discord par webhook : un embed par annonce.

L'URL du webhook est un SECRET : elle est lue dans .env (variable nommée dans config.yaml), jamais écrite
dans un message d'erreur ni dans le journal. Un webhook (salon) différent est possible par veille.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
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

COULEURS = {"🔥": 0xFF5722, "✅": 0x2ECC71, "🔹": 0x95A5A6}
MAX_TENTATIVES = 3
ATTENTE_MAX_429 = 30.0  # on n'attend jamais plus de 30 s dans un envoi : le renvoi reprendra plus tard


def _tronquer(texte: str, limite: int) -> str:
    return texte if len(texte) <= limite else texte[: limite - 1] + "…"


class CanalDiscord:
    nom = "discord"

    def __init__(
        self,
        cfg: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._dormir = dormir
        self._http = httpx.AsyncClient(timeout=cfg.discord.timeout_secondes, transport=transport)

    async def fermer(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ mise en forme

    @staticmethod
    def construire_embed(a: Alerte, maintenant: float) -> dict[str, Any]:
        champs: list[dict[str, Any]] = [
            {"name": "Marque", "value": a.marque or "—", "inline": True},
            {"name": "Taille", "value": a.taille or "—", "inline": True},
            {"name": "État", "value": libelle_etat(a.etat), "inline": True},
            {"name": "Prix affiché", "value": euros(a.prix), "inline": True},
        ]
        if not a.simple:
            champs += [
                {"name": "Coût réel", "value": f"{euros(a.cout_reel)}\n(frais + port inclus)", "inline": True},
                {"name": "Revente estimée", "value": libelle_revente(a), "inline": True},
                {"name": "Marge nette", "value": euros(a.marge, signe=True), "inline": True},
                {"name": "ROI", "value": pourcentage(a.roi_pct), "inline": True},
                {"name": "Deal score", "value": f"{a.palier} {a.score}/100", "inline": True},
            ]
        if a.tags:
            champs.append({"name": "Signaux", "value": _tronquer("\n".join(a.tags), 1024), "inline": False})
        champs.append({"name": "Vendeur", "value": libelle_vendeur(a.vendeur_note, a.vendeur_avis), "inline": True})
        champs.append({"name": "Publiée", "value": temps_ecoule(a.publie_le, maintenant), "inline": True})

        embed: dict[str, Any] = {
            "title": _tronquer(f"{a.palier} {a.titre}", 256),
            "url": a.url,
            "description": f"[Voir l'annonce sur Vinted]({a.url})",
            "color": COULEURS.get(a.palier, COULEURS["🔹"]),
            "fields": champs,
            "footer": {"text": _tronquer(f"{a.veille} · {a.domaine} · annonce {a.annonce_id}", 2048)},
        }
        if a.photo_url:
            embed["image"] = {"url": a.photo_url}
        if a.publie_le:
            embed["timestamp"] = datetime.fromtimestamp(a.publie_le, tz=timezone.utc).isoformat()
        return embed

    # ------------------------------------------------------------------ envoi

    def _url_webhook(self, nom_variable: str | None) -> str:
        nom = nom_variable or self._cfg.discord.webhook_defaut
        url = Config.secret(nom)
        if not url:
            raise ErreurEnvoi(f"variable d'environnement {nom} absente ou vide (.env)")
        return url

    async def envoyer_alerte(self, alerte: Alerte, maintenant: float) -> None:
        charge = {"username": self._cfg.discord.pseudo, "embeds": [self.construire_embed(alerte, maintenant)]}
        await self._poster(self._url_webhook(alerte.webhook), charge)

    async def envoyer_texte(self, message: str) -> None:
        charge = {"username": self._cfg.discord.pseudo, "content": _tronquer(message, 2000)}
        await self._poster(self._url_webhook(None), charge)

    async def _poster(self, url: str, charge: dict[str, Any]) -> None:
        """POST avec gestion de la limitation (429 : on respecte `retry_after`) et des erreurs temporaires."""
        derniere_erreur = "inconnue"
        for tentative in range(1, MAX_TENTATIVES + 1):
            try:
                reponse = await self._http.post(url, json=charge)
            except httpx.TransportError as e:
                derniere_erreur = f"erreur réseau ({type(e).__name__})"
            else:
                code = reponse.status_code
                if code in (200, 204):
                    return
                if code == 429:
                    attente = min(_retry_after(reponse), ATTENTE_MAX_429)
                    derniere_erreur = f"limitation Discord (HTTP 429, attente {attente:g} s)"
                    if tentative < MAX_TENTATIVES:
                        await self._dormir(attente)
                    continue
                if code >= 500:
                    derniere_erreur = f"erreur serveur Discord (HTTP {code})"
                else:  # 400, 401, 404… : webhook supprimé/invalide ou contenu refusé ; inutile de réessayer
                    raise ErreurEnvoi(f"Discord a refusé l'envoi (HTTP {code}) : webhook invalide ou contenu refusé")
            if tentative < MAX_TENTATIVES:
                await self._dormir(2.0 * tentative)
        raise ErreurEnvoi(f"échec après {MAX_TENTATIVES} tentatives : {derniere_erreur}")


def _retry_after(reponse: httpx.Response) -> float:
    try:
        valeur = reponse.json().get("retry_after")
    except (ValueError, AttributeError):
        valeur = None
    if valeur is None:
        valeur = reponse.headers.get("retry-after")
    try:
        return max(0.5, float(valeur))
    except (TypeError, ValueError):
        return 2.0
