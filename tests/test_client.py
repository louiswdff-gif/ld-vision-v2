"""Tests du client Vinted : transport simulé (httpx.MockTransport), horloge simulée, aucun réseau réel."""

from __future__ import annotations

import json

import httpx
import pytest

from bot_vinted.config import Reseau
from bot_vinted.vinted.client import (
    AnnonceIntrouvable,
    ClientVinted,
    ErreurReseau,
    VintedBloque,
)
from bot_vinted.vinted.url_recherche import RechercheEffective

COOKIE = "_vinted_fr_session=abc123; Domain=.vinted.fr; Path=/"


class Horloge:
    """Horloge simulée : `dormir` fait avancer le temps sans attendre."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def dormir(self, secondes: float) -> None:
        self.pauses.append(secondes)
        self.t += secondes


class FauxVinted:
    """Serveur simulé : enregistre les requêtes et répond selon un script."""

    def __init__(self, catalogue: dict, accueil: str, detail: dict, vendeur: dict) -> None:
        self.requetes: list[httpx.Request] = []
        self.catalogue, self.accueil, self.detail, self.vendeur = catalogue, accueil, detail, vendeur
        self.reponses_api: list[httpx.Response] = []  # réponses scriptées en priorité (une par appel d'API)

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        self.requetes.append(requete)
        if requete.url.path == "/":
            return httpx.Response(200, text=self.accueil, headers={"set-cookie": COOKIE, "content-type": "text/html"})
        if self.reponses_api:
            return self.reponses_api.pop(0)
        if requete.url.path == "/api/v2/catalog/items":
            return httpx.Response(200, json=self.catalogue)
        if requete.url.path.startswith("/api/v2/items/"):
            return httpx.Response(200, json=self.detail)
        if requete.url.path.startswith("/api/v2/users/"):
            return httpx.Response(200, json=self.vendeur)
        return httpx.Response(404)

    @property
    def chemins(self) -> list[str]:
        return [r.url.path for r in self.requetes]


@pytest.fixture
def horloge() -> Horloge:
    return Horloge()


@pytest.fixture
def serveur(charger_json, fixtures) -> FauxVinted:
    return FauxVinted(
        charger_json("catalogue.json"),
        (fixtures / "accueil.html").read_text(encoding="utf-8"),
        charger_json("detail_annonce.json"),
        charger_json("vendeur.json"),
    )


def creer_client(serveur: FauxVinted, horloge: Horloge, **reseau) -> ClientVinted:
    cfg = Reseau(**{"delai_min_entre_requetes_secondes": 2.0, **reseau})
    return ClientVinted(cfg, transport=httpx.MockTransport(serveur), horloge=horloge, dormir=horloge.dormir,
                        alea=lambda: 0.0)


def recherche(**kw) -> RechercheEffective:
    base = dict(domaine="vinted.fr", texte="carhartt veste", marques=[362], categories=[], tailles=[], couleurs=[],
                materiaux=[], etats=["tres_bon_etat"], prix_min=None, prix_max=40.0)
    base.update(kw)
    return RechercheEffective(**base)


# ------------------------------------------------------------------ cookie


async def test_cookie_obtenu_une_fois_puis_reutilise(serveur, horloge):
    async with creer_client(serveur, horloge) as client:
        annonces = await client.catalogue(recherche())
        await client.catalogue(recherche(), page=2)

    assert [a.id for a in annonces][:2] == [4001, 4002]
    assert serveur.chemins == ["/", "/api/v2/catalog/items", "/api/v2/catalog/items"]  # accueil UNE seule fois
    assert "_vinted_fr_session=abc123" in serveur.requetes[1].headers["cookie"]
    assert "_vinted_fr_session=abc123" in serveur.requetes[2].headers["cookie"]


async def test_cookie_renouvele_apres_expiration(serveur, horloge):
    async with creer_client(serveur, horloge, cookie_duree_max_minutes=10) as client:
        await client.catalogue(recherche())
        horloge.t += 11 * 60  # le cookie a plus de 10 minutes
        await client.catalogue(recherche())
    assert serveur.chemins == ["/", "/api/v2/catalog/items", "/", "/api/v2/catalog/items"]


async def test_403_renouvelle_le_cookie_puis_une_seule_nouvelle_tentative(serveur, horloge):
    serveur.reponses_api = [httpx.Response(403, text="forbidden")]  # 1er appel refusé, le second passe
    async with creer_client(serveur, horloge) as client:
        annonces = await client.catalogue(recherche())
    assert len(annonces) == 6
    assert serveur.chemins == ["/", "/api/v2/catalog/items", "/", "/api/v2/catalog/items"]


async def test_403_persistant_leve_vinted_bloque_sans_boucler(serveur, horloge):
    serveur.reponses_api = [httpx.Response(403, text="x"), httpx.Response(403, text="x"), httpx.Response(200, json={})]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(VintedBloque) as e:
            await client.catalogue(recherche())
    assert e.value.statut == 403
    assert serveur.chemins.count("/api/v2/catalog/items") == 2  # exactement 2 tentatives, pas plus


async def test_401_traite_comme_un_cookie_expire(serveur, horloge):
    serveur.reponses_api = [httpx.Response(401, json={"message": "unauthorized"})]
    async with creer_client(serveur, horloge) as client:
        assert len(await client.catalogue(recherche())) == 6


# ------------------------------------------------------------------ blocages et pannes


async def test_429_avec_retry_after(serveur, horloge):
    serveur.reponses_api = [httpx.Response(429, headers={"retry-after": "120"}, text="slow down")]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(VintedBloque) as e:
            await client.catalogue(recherche())
    assert e.value.statut == 429 and e.value.attente == 120
    assert serveur.chemins.count("/api/v2/catalog/items") == 1  # jamais de nouvelle tentative sur un 429


async def test_429_sans_retry_after(serveur, horloge):
    serveur.reponses_api = [httpx.Response(429)]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(VintedBloque) as e:
            await client.catalogue(recherche())
    assert e.value.attente is None


async def test_page_de_defi_antibot_est_traitee_comme_un_blocage(serveur, horloge, fixtures):
    html = (fixtures / "defi_antibot.html").read_text(encoding="utf-8")
    serveur.reponses_api = [httpx.Response(200, text=html, headers={"content-type": "text/html"})]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(VintedBloque, match="anti-bot"):
            await client.catalogue(recherche())


async def test_erreur_serveur_5xx_est_une_erreur_reseau(serveur, horloge):
    serveur.reponses_api = [httpx.Response(503)]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(ErreurReseau):
            await client.catalogue(recherche())


async def test_timeout_et_coupure_sont_des_erreurs_reseau(horloge):
    def timeout(requete):
        raise httpx.ReadTimeout("trop lent", request=requete)

    client = ClientVinted(Reseau(), transport=httpx.MockTransport(timeout), horloge=horloge, dormir=horloge.dormir)
    with pytest.raises(ErreurReseau, match="délai dépassé"):
        await client.catalogue(recherche())
    await client.fermer()

    def coupure(requete):
        raise httpx.ConnectError("refusé", request=requete)

    client = ClientVinted(Reseau(), transport=httpx.MockTransport(coupure), horloge=horloge, dormir=horloge.dormir)
    with pytest.raises(ErreurReseau, match="erreur réseau"):
        await client.catalogue(recherche())
    await client.fermer()


async def test_accueil_bloque_leve_vinted_bloque(horloge):
    client = ClientVinted(Reseau(), transport=httpx.MockTransport(lambda r: httpx.Response(403, text="captcha")),
                          horloge=horloge, dormir=horloge.dormir)
    with pytest.raises(VintedBloque):
        await client.catalogue(recherche())
    await client.fermer()


# ------------------------------------------------------------------ cadence


async def test_delai_minimal_entre_deux_requetes(serveur, horloge):
    async with creer_client(serveur, horloge) as client:
        await client.catalogue(recherche())
        await client.catalogue(recherche())
    # accueil → catalogue → catalogue : 2 attentes d'au moins 2 s (aucune avant la toute première requête)
    assert len(horloge.pauses) == 2 and all(p >= 2.0 for p in horloge.pauses)


async def test_pas_d_attente_si_le_delai_est_deja_ecoule(serveur, horloge):
    async with creer_client(serveur, horloge, delai_min_entre_requetes_secondes=2.0) as client:
        await client.catalogue(recherche())
        horloge.pauses.clear()
        horloge.t += 60  # longtemps après
        await client.catalogue(recherche())
    assert horloge.pauses == []


async def test_une_seule_requete_a_la_fois(serveur, horloge):
    import asyncio

    en_cours = 0
    max_concurrent = 0

    def handler(requete: httpx.Request) -> httpx.Response:
        return serveur(requete)

    class TransportLent(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal en_cours, max_concurrent
            en_cours += 1
            max_concurrent = max(max_concurrent, en_cours)
            await asyncio.sleep(0.01)
            en_cours -= 1
            return handler(request)

    client = ClientVinted(Reseau(delai_min_entre_requetes_secondes=0), transport=TransportLent(), horloge=horloge,
                          dormir=horloge.dormir)
    await asyncio.gather(*(client.catalogue(recherche()) for _ in range(5)))
    await client.fermer()
    assert max_concurrent == 1


# ------------------------------------------------------------------ contenu des requêtes


async def test_parametres_et_en_tetes_envoyes(serveur, horloge):
    cfg_ua = "Mozilla/5.0 (test) UA-fixe"
    async with creer_client(serveur, horloge, user_agent=cfg_ua, accept_language="fr-FR", articles_par_page=24) as client:
        await client.catalogue(recherche(categories=[2050], tailles=[207, 208]), page=3)

    api = serveur.requetes[1]
    assert dict(api.url.params) == {
        "order": "newest_first", "page": "3", "per_page": "24", "search_text": "carhartt veste",
        "brand_ids": "362", "catalog_ids": "2050", "size_ids": "207,208",
    }
    # prix et états sont filtrés côté bot : jamais envoyés à Vinted
    assert "price_to" not in api.url.params and "status_ids" not in api.url.params
    for requete in serveur.requetes:  # accueil comme API : même User-Agent fixe
        assert requete.headers["user-agent"] == cfg_ua
        assert requete.headers["accept-language"] == "fr-FR"
    assert "application/json" in api.headers["accept"]
    assert str(api.url).startswith("https://www.vinted.fr/api/v2/catalog/items")


# ------------------------------------------------------------------ détail et vendeur


async def test_detail_annonce(serveur, horloge):
    async with creer_client(serveur, horloge) as client:
        detail = await client.detail("vinted.fr", 4001)
    assert detail.nb_photos == 5 and detail.vendeur.nb_avis == 142
    assert serveur.chemins[-1] == "/api/v2/items/4001"


async def test_detail_introuvable_retourne_none(serveur, horloge):
    serveur.reponses_api = [httpx.Response(404, json={"message": "not found"})]
    async with creer_client(serveur, horloge) as client:
        assert await client.detail("vinted.fr", 999) is None


async def test_vendeur(serveur, horloge):
    async with creer_client(serveur, horloge) as client:
        v = await client.vendeur("vinted.fr", 77)
        brut = await client.brut("vinted.fr", "/api/v2/users/77")
    assert v.note == pytest.approx(4.8) and v.nb_avis == 142 and v.pays == "FR"
    assert json.dumps(brut)  # le JSON brut est exploitable tel quel (commande `capturer`)


async def test_vendeur_introuvable(serveur, horloge):
    serveur.reponses_api = [httpx.Response(404)]
    async with creer_client(serveur, horloge) as client:
        with pytest.raises(AnnonceIntrouvable):
            await client.brut("vinted.fr", "/api/v2/users/1")


async def test_cookies_separes_par_domaine(serveur, horloge):
    async with creer_client(serveur, horloge) as client:
        await client.catalogue(recherche(domaine="vinted.fr"))
        await client.catalogue(recherche(domaine="vinted.be"))
    hotes = [r.url.host for r in serveur.requetes]
    assert hotes == ["www.vinted.fr", "www.vinted.fr", "www.vinted.be", "www.vinted.be"]
    assert serveur.chemins == ["/", "/api/v2/catalog/items", "/", "/api/v2/catalog/items"]
