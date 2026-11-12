"""Client HTTP asynchrone pour le site web public de Vinted (sans compte).

Règles respectées par construction :
- UNE requête à la fois (un verrou englobe cookie + requête + éventuel renouvellement) ;
- délai minimal entre deux requêtes, toutes veilles confondues ;
- cookie de session anonyme obtenu en visitant la page d'accueil, renouvelé à l'expiration
  (ou une fois si Vinted répond 401/403) ;
- AUCUN contournement : pas de proxy, pas de rotation, pas d'imitation d'empreinte TLS/navigateur,
  pas de CAPTCHA. Un 429/403 persistant ou un défi anti-bot lève `VintedBloque` : l'appelant ralentit,
  met en pause et prévient. Le User-Agent est un simple en-tête fixe et configurable.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from bot_vinted.config import Reseau
from bot_vinted.vinted.modeles import Annonce, Vendeur, analyser_detail, analyser_vendeur
from bot_vinted.vinted.page_catalogue import PageInattendue, analyser_page
from bot_vinted.vinted.url_recherche import RechercheEffective

log = logging.getLogger(__name__)


class ErreurVinted(Exception):
    """Erreur générique d'accès à Vinted."""


class VintedBloque(ErreurVinted):
    """429 / 403 persistant / défi anti-bot : il faut ralentir et se mettre en pause (jamais insister)."""

    def __init__(self, motif: str, statut: int | None = None, attente: float | None = None) -> None:
        super().__init__(motif)
        self.motif = motif
        self.statut = statut
        self.attente = attente  # secondes demandées par le serveur (Retry-After), si fournies


class ErreurReseau(ErreurVinted):
    """Timeout, connexion coupée ou erreur 5xx : probablement temporaire."""


class AnnonceIntrouvable(ErreurVinted):
    """404 : annonce supprimée, vendue ou masquée."""


class ClientVinted:
    def __init__(
        self,
        reseau: Reseau,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        horloge: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alea: Callable[[], float] = random.random,
    ) -> None:
        self._cfg = reseau
        self._horloge = horloge
        self._dormir = dormir
        self._alea = alea
        self._http = httpx.AsyncClient(
            timeout=reseau.timeout_secondes,
            follow_redirects=True,
            transport=transport,
        )
        self._verrou = asyncio.Lock()
        self._derniere_fin: float | None = None  # instant de fin de la dernière requête
        self._cookie_obtenu_le: dict[str, float] = {}

    async def __aenter__(self) -> "ClientVinted":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.fermer()

    async def fermer(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ API publique

    async def catalogue(self, recherche: RechercheEffective, page: int = 1) -> list[Annonce]:
        """Une page de résultats (plus récentes d'abord), lue dans la page web `/catalog` de Vinted."""
        html = await self.texte(recherche.domaine, "/catalog", recherche.parametres_page(page))
        try:
            resultat = analyser_page(html, recherche.domaine)
        except PageInattendue as e:
            # Un format inattendu doit se voir (et déclencher la pause), pas passer pour « aucune nouvelle annonce ».
            raise ErreurVinted(f"page de recherche inexploitable : {e}") from e
        if resultat.brutes and not resultat.annonces:
            raise ErreurVinted(f"aucune des {resultat.brutes} annonces de la page n'est lisible : "
                               "le format de Vinted a peut-être changé")
        return resultat.annonces

    async def detail(self, domaine: str, annonce_id: int) -> Annonce | None:
        """Détail complet d'une annonce (description, photos, vendeur). None si introuvable."""
        try:
            donnees = await self.brut(domaine, f"/api/v2/items/{annonce_id}")
        except AnnonceIntrouvable:
            return None
        try:
            return analyser_detail(donnees, domaine)
        except (ValueError, TypeError, AttributeError) as e:
            raise ErreurVinted(f"détail de l'annonce {annonce_id} illisible : {e}") from e

    async def vendeur(self, domaine: str, user_id: int) -> Vendeur | None:
        """Profil public d'un vendeur (note, avis, pays). None si introuvable."""
        try:
            donnees = await self.brut(domaine, f"/api/v2/users/{user_id}")
        except AnnonceIntrouvable:
            return None
        return analyser_vendeur(donnees.get("user") if isinstance(donnees.get("user"), dict) else donnees)

    async def brut(self, domaine: str, chemin: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET JSON sur `https://www.<domaine><chemin>`, avec cookie, cadence et gestion des blocages."""
        reponse = await self._requete(domaine, chemin, params, html=False)
        return self._interpreter(reponse, chemin)

    async def texte(self, domaine: str, chemin: str, params: list[tuple[str, str]] | None = None) -> str:
        """GET d'une PAGE web (HTML) : mêmes règles de politesse que `brut`."""
        reponse = await self._requete(domaine, chemin, params, html=True)
        self._verifier_statut(reponse, chemin)
        return reponse.text

    async def _requete(self, domaine: str, chemin: str, params, *, html: bool) -> httpx.Response:
        url = f"https://www.{domaine}{chemin}"
        async with self._verrou:
            await self._assurer_cookie(domaine)
            reponse = await self._get(url, params, html=html)
            if reponse.status_code in (401, 403):
                log.info("Vinted a répondu %s : renouvellement du cookie de session puis une seule nouvelle tentative.",
                         reponse.status_code)
                await self._renouveler_cookie(domaine)
                reponse = await self._get(url, params, html=html)
            return reponse

    # ------------------------------------------------------------------ cookie

    async def _assurer_cookie(self, domaine: str) -> None:
        obtenu = self._cookie_obtenu_le.get(domaine)
        if obtenu is None or self._horloge() - obtenu > self._cfg.cookie_duree_max_minutes * 60:
            await self._renouveler_cookie(domaine)

    async def _renouveler_cookie(self, domaine: str) -> None:
        """Visite la page d'accueil : Vinted dépose alors un cookie de session anonyme."""
        self._vider_cookies(domaine)
        reponse = await self._get(f"https://www.{domaine}/", None, html=True)
        if reponse.status_code == 200:
            self._cookie_obtenu_le[domaine] = self._horloge()
            log.debug("Cookie de session anonyme obtenu pour %s.", domaine)
            return
        # Même signification qu'une réponse d'API : blocage, limitation ou panne.
        self._verifier_statut(reponse, "/")
        raise ErreurReseau(f"page d'accueil {domaine} : HTTP {reponse.status_code}")

    def _vider_cookies(self, domaine: str) -> None:
        jar = self._http.cookies.jar
        for c in list(jar):
            if c.domain.lstrip(".").endswith(domaine):
                jar.clear(c.domain, c.path, c.name)
        self._cookie_obtenu_le.pop(domaine, None)

    # ------------------------------------------------------------------ requête et interprétation

    async def _attendre_son_tour(self) -> None:
        """Garantit un délai minimal (+ petit jitter) depuis la fin de la requête précédente."""
        if self._derniere_fin is None:
            return
        delai = self._cfg.delai_min_entre_requetes_secondes
        reste = delai - (self._horloge() - self._derniere_fin)
        if reste > 0:
            await self._dormir(reste + self._alea() * 0.3 * delai)

    async def _get(self, url: str, params, *, html: bool) -> httpx.Response:
        await self._attendre_son_tour()
        entetes = {
            "User-Agent": self._cfg.user_agent,
            "Accept-Language": self._cfg.accept_language,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8" if html else "application/json, text/plain, */*",
        }
        debut = self._horloge()
        try:
            reponse = await self._http.get(url, params=params, headers=entetes)
        except httpx.TimeoutException as e:
            self._derniere_fin = self._horloge()
            raise ErreurReseau(f"délai dépassé ({self._cfg.timeout_secondes:g} s) sur {url}") from e
        except httpx.TransportError as e:
            self._derniere_fin = self._horloge()
            raise ErreurReseau(f"erreur réseau ({type(e).__name__}) sur {url}") from e
        self._derniere_fin = self._horloge()
        log.debug("GET %s %s → %s (%.1f s)", url, params or "", reponse.status_code, self._derniere_fin - debut)
        return reponse

    @staticmethod
    def _verifier_statut(reponse: httpx.Response, chemin: str) -> None:
        """Ne laisse passer que les réponses 200 ; sinon lève l'erreur correspondante (blocage, 404, panne…)."""
        code = reponse.status_code
        if code == 200:
            return
        if code == 404:
            raise AnnonceIntrouvable(f"HTTP 404 pour {chemin}")
        if code in (401, 403):
            raise VintedBloque(f"accès refusé (HTTP {code})", statut=code)
        if code == 429:
            raise VintedBloque("trop de requêtes (HTTP 429)", statut=code, attente=_retry_after(reponse))
        if code >= 500:
            raise ErreurReseau(f"erreur serveur Vinted (HTTP {code})")
        raise ErreurVinted(f"réponse inattendue HTTP {code} pour {chemin}")

    @classmethod
    def _interpreter(cls, reponse: httpx.Response, chemin: str) -> dict[str, Any]:
        cls._verifier_statut(reponse, chemin)
        try:
            donnees = reponse.json()
        except ValueError as e:
            # 200 mais pas du JSON : page HTML de vérification anti-bot → on la traite comme un 403.
            raise VintedBloque("réponse non JSON (probable défi anti-bot)", statut=reponse.status_code) from e
        if not isinstance(donnees, dict):
            raise ErreurVinted(f"réponse JSON inattendue pour {chemin}")
        return donnees


def _retry_after(reponse: httpx.Response) -> float | None:
    """En-tête Retry-After en secondes (la forme « date HTTP » est ignorée)."""
    brut = reponse.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(brut))
    except ValueError:
        return None
