"""Tests de bout en bout du moteur contre un FAUX Vinted (transport simulé, horloge simulée, aucun réseau réel)."""

from __future__ import annotations

import httpx
import pytest

from bot_vinted.alertes import ErreurEnvoi, construire_diffuseur
from bot_vinted.alertes.base import Diffuseur
from bot_vinted.base import Base
from bot_vinted.config import Config, Veille
from bot_vinted.marques import CatalogueMarques
from bot_vinted.moteur import Moteur
from bot_vinted.vinted.client import ClientVinted

from tests.conftest import RACINE_PROJET

DEBUT = 1_731_000_000.0


class Monde:
    """Faux Vinted + horloge : sert le catalogue/détails configurés, enregistre les requêtes."""

    def __init__(self) -> None:
        self.t = DEBUT
        self.catalogue: list[dict] = []
        self.details: dict[int, dict] = {}
        self.scenario: list[httpx.Response] = []  # réponses forcées pour les appels d'API (consommées dans l'ordre)
        self.requetes: list[httpx.Request] = []

    # --- horloge
    def __call__(self) -> float:
        return self.t

    async def dormir(self, secondes: float) -> None:
        self.t += secondes

    # --- annonces
    def item(self, ident: int, titre: str = "Veste Carhartt Detroit", prix: float = 25.0, marque: str = "Carhartt",
             taille: str = "L", etat: str = "Très bon état", age_min: float = 2.0, vendeur: int = 77) -> dict:
        return {
            "id": ident, "title": titre, "price": {"amount": str(prix), "currency_code": "EUR"},
            "brand_title": marque, "size_title": taille, "status": etat,
            "url": f"https://www.vinted.fr/items/{ident}-article",
            "photo": {"url": f"https://img.vinted/{ident}.jpg",
                      "high_resolution": {"timestamp": int(self.t - age_min * 60)}},
            "user": {"id": vendeur, "login": f"vendeur{vendeur}"},
        }

    def detail(self, ident: int, description: str = "Très belle veste, envoi soigné.", photos: int = 4,
               note: float = 0.96, avis: int = 50, pays: str = "FR") -> dict:
        return {"item": {
            "id": ident, "title": "x", "price": {"amount": "1"}, "description": description,
            "photos": [{"url": f"https://img.vinted/{ident}-{i}.jpg"} for i in range(photos)],
            "user": {"id": 77, "login": "vendeur77", "feedback_reputation": note, "feedback_count": avis,
                     "country_iso_code": pays},
        }}

    # --- serveur simulé
    def handler(self, requete: httpx.Request) -> httpx.Response:
        self.requetes.append(requete)
        chemin = requete.url.path
        if chemin == "/":
            return httpx.Response(200, text="<html>accueil</html>", headers={"set-cookie": "_s=1; Domain=.vinted.fr; Path=/"})
        if self.scenario:
            return self.scenario.pop(0)
        if chemin == "/api/v2/catalog/items":
            page, par_page = int(requete.url.params["page"]), int(requete.url.params["per_page"])
            return httpx.Response(200, json={"items": self.catalogue[(page - 1) * par_page: page * par_page]})
        if chemin.startswith("/api/v2/items/"):
            ident = int(chemin.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.details.get(ident) or self.detail(ident))
        return httpx.Response(404)

    def chemins(self, prefixe: str) -> list[str]:
        return [r.url.path for r in self.requetes if r.url.path.startswith(prefixe)]

    def recherches(self) -> list[httpx.QueryParams]:
        return [r.url.params for r in self.requetes if r.url.path == "/api/v2/catalog/items"]


class CanalMemoire:
    nom = "memoire"

    def __init__(self) -> None:
        self.alertes: list = []
        self.textes: list[str] = []
        self.echoue = False

    async def envoyer_alerte(self, alerte, maintenant) -> None:
        if self.echoue:
            raise ErreurEnvoi("panne simulée")
        self.alertes.append(alerte)

    async def envoyer_texte(self, message: str) -> None:
        if self.echoue:
            raise ErreurEnvoi("panne simulée")
        self.textes.append(message)

    async def fermer(self) -> None:
        return None


def veille(**kw) -> Veille:
    d = dict(
        nom="Carhartt vestes", url_recherche="https://www.vinted.fr/catalog?search_text=carhartt%20veste", prix_max=40,
        etats=["neuf_avec_etiquette", "neuf_sans_etiquette", "tres_bon_etat"], marge_min=20, score_min=60,
        port_estime=5.0, prix_revente_cible=70, mots_exclus=["enfant"], vendeur_note_min=4.5, vendeur_avis_min=5,
    )
    d.update(kw)
    return Veille(**d)


def config(*veilles: Veille, **kw) -> Config:
    d = dict(
        veilles=list(veilles) or [veille()], pages_amorcage=3, premier_lancement_minutes=15,
        reseau={"articles_par_page": 5, "delai_min_entre_requetes_secondes": 0, "max_details_par_cycle": 10,
                "backoff": {"initial_secondes": 60, "facteur": 2, "max_secondes": 1800, "alerte_apres": 2,
                            "jitter_pct": 0}},
    )
    d.update(kw)
    return Config(**d)


class Banc:
    """Assemble moteur + faux monde + base mémoire + canal mémoire."""

    def __init__(self, monde: Monde, cfg: Config, dry_run: bool = False, diffuseur: Diffuseur | None = None) -> None:
        self.monde, self.cfg = monde, cfg
        self.base = Base(":memory:")
        self.canal = CanalMemoire()
        self.client = ClientVinted(
            cfg.reseau, transport=httpx.MockTransport(monde.handler), horloge=monde, dormir=monde.dormir,
            alea=lambda: 0.0,
        )
        self.attentes: list[float] = []
        self.arret_apres: int | None = None

        async def attendre(secondes: float) -> bool:
            self.attentes.append(secondes)
            monde.t += secondes
            if self.arret_apres is not None and len(self.attentes) >= self.arret_apres:
                self.moteur.demander_arret()
                return True
            return False

        self.moteur = Moteur(
            cfg, self.base, self.client, diffuseur or Diffuseur([self.canal]),
            CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", cfg.marques),
            dry_run=dry_run, horloge=monde, attendre=attendre, alea=lambda: 0.0, installer_signaux=False,
        )

    async def passage(self, indice: int = 0) -> None:
        """Exécute UN passage de la recherche `indice` (planifie si nécessaire)."""
        if not self.moteur.plans:
            self.moteur.plans = self.moteur._construire_plans()
        await self.moteur._executer(self.moteur.plans[indice])

    async def fermer(self) -> None:
        await self.client.fermer()
        self.base.fermer()


@pytest.fixture
def monde() -> Monde:
    return Monde()


@pytest.fixture
async def banc(monde):
    b = Banc(monde, config())
    yield b
    await b.fermer()


def catalogue_amorcage(monde: Monde) -> list[dict]:
    """13 annonces, les plus récentes d'abord : 1 bonne affaire fraîche, 1 trop chère, 1 fausse, 10 anciennes à 70 €."""
    return [
        monde.item(200, prix=25.0, age_min=2),                                   # bonne affaire → alerte
        monde.item(201, "Veste Carhartt Active", prix=45.0, age_min=3),          # > prix_max : écartée mais historisée
        monde.item(202, "Veste type Carhartt", prix=10.0, age_min=4),            # contrefaçon : écartée, PAS historisée
        *[monde.item(100 + i, prix=70.0, age_min=120 + i) for i in range(10)],   # anciennes : références de prix
    ]


# =============================================================================== premier passage


async def test_premier_passage_amorce_l_historique_et_n_alerte_que_sur_le_recent(monde, banc):
    monde.catalogue = catalogue_amorcage(monde)
    await banc.passage()

    # 3 pages lues (13 annonces, 5 par page) avant toute évaluation
    assert len(monde.recherches()) == 3
    assert [p["page"] for p in monde.recherches()] == ["1", "2", "3"]

    # une seule alerte : la bonne affaire fraîche ; valorisée avec la MÉDIANE des références amorcées
    assert [a.annonce_id for a in banc.canal.alertes] == [200]
    a = banc.canal.alertes[0]
    assert a.source_revente == "mediane" and a.nb_references == 11  # 10 × 70 € + l'annonce à 45 € (pas elle-même)
    assert a.revente_estimee == 59.5 and a.cout_reel == 31.95 and a.marge == 27.55
    assert a.palier == "🔥" and 80 <= a.score <= 95
    assert a.veille == "Carhartt vestes" and a.vendeur_avis == 50 and a.vendeur_note == pytest.approx(4.8)

    # historique : tout sauf la contrefaçon (et les filtres de prix ne l'appauvrissent pas)
    refs = banc.base.prix_references("vinted.fr", "carhartt", "0", "l", "tres_bon_etat", 0)
    assert sorted(refs) == sorted([25.0, 45.0] + [70.0] * 10)

    # les annonces anciennes sont marquées « vues » sans alerte ; un seul détail chargé
    assert all(banc.base.est_vue("vinted.fr", 100 + i) for i in range(10))
    assert monde.chemins("/api/v2/items/") == ["/api/v2/items/200"]
    assert banc.base.etat_veille("Carhartt vestes") is not None


async def test_aucune_alerte_sur_l_existant_meme_tres_rentable(monde, banc):
    # toutes les annonces ont plus de 15 minutes : ce sont des affaires, mais « l'existant » n'alerte pas
    monde.catalogue = [monde.item(i, prix=10.0, age_min=60) for i in range(1, 4)]
    await banc.passage()
    assert banc.canal.alertes == [] and monde.chemins("/api/v2/items/") == []
    assert all(banc.base.est_vue("vinted.fr", i) for i in (1, 2, 3))


async def test_annonce_sans_date_ignoree_au_premier_passage(monde, banc):
    sans_date = monde.item(1, prix=10.0)
    sans_date["photo"] = None
    monde.catalogue = [sans_date]
    await banc.passage()
    assert banc.canal.alertes == []


# =============================================================================== passages suivants


async def test_pas_de_doublon_au_deuxieme_passage(monde, banc):
    monde.catalogue = catalogue_amorcage(monde)
    await banc.passage()
    details_avant = len(monde.chemins("/api/v2/items/"))
    monde.t += 60
    await banc.passage()  # exactement le même catalogue
    assert len(banc.canal.alertes) == 1  # jamais deux alertes pour la même annonce
    assert len(monde.chemins("/api/v2/items/")) == details_avant  # et aucun détail rechargé
    assert len(monde.recherches()) == 4  # 3 pages d'amorçage, puis UNE page


async def test_nouvelle_annonce_au_passage_suivant(monde, banc):
    monde.catalogue = catalogue_amorcage(monde)
    await banc.passage()
    monde.t += 60
    monde.catalogue = [monde.item(300, "Veste Carhartt Chore", prix=22.0, age_min=1)] + monde.catalogue
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [200, 300]
    # au passage suivant l'ancienneté n'est plus limitée à 15 minutes (pas « premier passage »)
    monde.t += 60
    monde.catalogue = [monde.item(301, "Veste Carhartt Chore", prix=22.0, age_min=300)] + monde.catalogue
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [200, 300, 301]


# =============================================================================== filtres


async def test_filtres_vendeur_description_photos_apres_detail(monde):
    cfg = config(veille(photos_min=3))
    banc = Banc(monde, cfg)
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)  # pas un premier passage
    monde.catalogue = [monde.item(i, prix=25.0) for i in (1, 2, 3, 4, 5)]
    monde.details = {
        1: monde.detail(1),                                    # OK
        2: monde.detail(2, note=0.80),                         # note 4,0 < 4,5
        3: monde.detail(3, avis=2),                            # 2 avis < 5
        4: monde.detail(4, photos=2),                          # 2 photos < 3
        5: monde.detail(5, description="petit trou à la manche"),  # défaut dans la description
    }
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [1]
    assert len(monde.chemins("/api/v2/items/")) == 5
    await banc.fermer()


async def test_seuils_marge_et_score(monde):
    banc = Banc(monde, config(veille(marge_min=30, score_min=60)))
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]  # marge = 70 − 31,95 = 38,05 → passe marge_min=30
    await banc.passage()
    assert len(banc.canal.alertes) == 1
    await banc.fermer()

    monde2 = Monde()
    banc2 = Banc(monde2, config(veille(marge_min=45)))  # marge 38,05 < 45 : écartée AVANT le détail
    banc2.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde2.catalogue = [monde2.item(1, prix=25.0)]
    await banc2.passage()
    assert banc2.canal.alertes == [] and monde2.chemins("/api/v2/items/") == []  # aucune requête de détail gaspillée
    await banc2.fermer()

    monde3 = Monde()
    banc3 = Banc(monde3, config(veille(score_min=99)))  # score trop faible : écartée APRÈS le détail
    banc3.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde3.catalogue = [monde3.item(1, prix=25.0)]
    await banc3.passage()
    assert banc3.canal.alertes == [] and len(monde3.chemins("/api/v2/items/")) == 1
    await banc3.fermer()


async def test_prix_et_etat_filtres_cote_client(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [
        monde.item(1, prix=41.0),                      # > prix_max (40)
        monde.item(2, prix=25.0, etat="Bon état"),     # état non souhaité
        monde.item(3, "Veste Carhartt enfant", prix=25.0),  # mot exclu de la veille
        monde.item(4, prix=25.0),                      # OK
    ]
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [4]
    # ni le prix ni l'état ne sont envoyés à Vinted
    assert all("price_to" not in p and "status_ids" not in p for p in monde.recherches())


async def test_annonce_non_estimable_sans_references_ni_cible(monde):
    banc = Banc(monde, config(veille(prix_revente_cible=None)))
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=10.0)]
    await banc.passage()
    assert banc.canal.alertes == [] and monde.chemins("/api/v2/items/") == []
    await banc.fermer()


async def test_annonce_disparue_pendant_le_detail(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    monde.scenario = [httpx.Response(200, json={"items": monde.catalogue}), httpx.Response(404)]  # catalogue puis détail 404
    await banc.passage()
    assert banc.canal.alertes == [] and banc.base.est_vue("vinted.fr", 1)


# =============================================================================== budget de détails


async def test_budget_de_details_reporte_le_reste_au_passage_suivant(monde):
    cfg = config(reseau={"articles_par_page": 5, "delai_min_entre_requetes_secondes": 0, "max_details_par_cycle": 1,
                         "backoff": {"jitter_pct": 0}})
    banc = Banc(monde, cfg)
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0), monde.item(2, prix=24.0)]
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [1]
    assert not banc.base.est_vue("vinted.fr", 2)  # PAS perdue : réexaminée au passage suivant
    monde.t += 60
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [1, 2]
    await banc.fermer()


# =============================================================================== pause et reprise


async def test_pause_sur_429_une_seule_alerte_puis_reprise(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    monde.scenario = [httpx.Response(429, headers={"retry-after": "5"}) for _ in range(3)]
    banc.arret_apres = 8

    await banc.moteur.lancer()

    # backoff exponentiel : 60 s, 120 s, 240 s entre les 3 échecs (aucune attente ne descend sous le backoff)
    pauses = [a for a in banc.attentes if a >= 60]
    assert pauses[:3] == [60.0, 120.0, 240.0]
    # UN SEUL message « bot en pause » (seuil : 2 échecs), même après le 3ᵉ échec
    en_pause = [t for t in banc.canal.textes if "en pause" in t]
    assert len(en_pause) == 1 and "429" in en_pause[0]
    # dès que Vinted répond de nouveau : UN message de reprise, puis fonctionnement normal et alerte envoyée
    reprises = [t for t in banc.canal.textes if "reprise" in t.lower()]
    assert len(reprises) == 1
    assert [a.annonce_id for a in banc.canal.alertes] == [1]
    assert banc.moteur.pause.echecs == 0


async def test_pas_de_message_si_un_seul_echec_isole(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    monde.scenario = [httpx.Response(503)]
    await banc.passage()
    assert banc.canal.textes == []  # un incident isolé ne dérange pas
    monde.t += 200
    await banc.passage()
    assert banc.canal.textes == [] and len(banc.canal.alertes) == 1  # reprise silencieuse (aucune alerte de pause envoyée)


async def test_retry_after_plus_long_que_le_backoff(monde, banc):
    monde.scenario = [httpx.Response(429, headers={"retry-after": "600"})]
    await banc.passage()
    assert banc.moteur._pause_jusqu_a - monde.t == pytest.approx(600)


async def test_une_annonce_n_est_pas_perdue_si_un_blocage_survient_pendant_le_detail(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    monde.scenario = [httpx.Response(200, json={"items": monde.catalogue}), httpx.Response(429)]  # catalogue OK, détail 429
    await banc.passage()
    assert banc.canal.alertes == [] and not banc.base.est_vue("vinted.fr", 1)
    monde.t += 500
    await banc.passage()  # réessai : l'annonce est réexaminée et alertée
    assert [a.annonce_id for a in banc.canal.alertes] == [1]


async def test_defi_antibot_traite_comme_un_blocage(monde, banc):
    monde.scenario = [httpx.Response(200, text="<html>verifiez que vous etes humain</html>")]
    await banc.passage()
    assert banc.moteur.pause.echecs == 1 and banc.moteur._pause_jusqu_a > monde.t


# =============================================================================== alertes en attente et dry-run


async def test_alerte_conservee_puis_renvoyee_sans_doublon(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    banc.canal.echoue = True
    await banc.passage()
    assert banc.canal.alertes == []
    assert banc.base.annonce("vinted.fr", 1)["alerte_statut"] == "en_attente"  # conservée en base

    banc.canal.echoue = False
    monde.t += 120
    await banc.moteur._renvoyer_alertes_en_attente()
    assert [a.annonce_id for a in banc.canal.alertes] == [1]
    assert banc.base.annonce("vinted.fr", 1)["alerte_statut"] == "envoyee"
    a = banc.canal.alertes[0]
    assert a.palier in ("🔥", "✅") and a.cout_reel == 31.95 and a.webhook == "DISCORD_WEBHOOK_URL"

    monde.t += 60
    await banc.passage()  # l'annonce est toujours au catalogue : aucun doublon
    await banc.moteur._renvoyer_alertes_en_attente()
    assert len(banc.canal.alertes) == 1


async def test_renvoi_differe_apres_un_nouvel_echec(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    banc.canal.echoue = True
    await banc.passage()
    await banc.moteur._renvoyer_alertes_en_attente()  # trop tôt (échec d'envoi < 60 s) : rien n'est tenté
    prochain = banc.moteur._prochain_renvoi
    assert prochain > monde.t
    banc.canal.echoue = False
    await banc.moteur._renvoyer_alertes_en_attente()  # trop tôt : rien n'est tenté
    assert banc.canal.alertes == []
    monde.t = prochain + 1
    await banc.moteur._renvoyer_alertes_en_attente()
    assert len(banc.canal.alertes) == 1


async def test_dry_run_affiche_en_console_et_n_envoie_rien(monde, capsys):
    cfg = config()
    banc = Banc(monde, cfg, dry_run=True, diffuseur=construire_diffuseur(cfg, dry_run=True))
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]
    await banc.passage()
    sortie = capsys.readouterr().out
    assert "DRY-RUN" in sortie and "Veste Carhartt Detroit" in sortie and "https://www.vinted.fr/items/1" in sortie
    assert banc.base.annonce("vinted.fr", 1)["alerte_statut"] == "simulee"
    assert banc.base.alertes_en_attente() == []
    assert all(r.url.host == "www.vinted.fr" for r in monde.requetes)  # aucune requête vers Discord/Telegram
    await banc.fermer()


# =============================================================================== planification


async def test_une_fois_execute_chaque_recherche_une_seule_fois(monde):
    cfg = config(veille(nom="A"), veille(nom="B", url_recherche="https://www.vinted.fr/catalog?search_text=nike"))
    banc = Banc(monde, cfg)
    for nom in ("A", "B"):
        banc.base.marquer_passage(nom, DEBUT - 1000)
    monde.catalogue = []
    await banc.moteur.lancer(une_fois=True)
    textes = [p["search_text"] for p in monde.recherches()]
    assert textes == ["carhartt veste", "nike"]
    await banc.fermer()


async def test_une_requete_a_la_fois_et_intervalles_par_veille(monde):
    cfg = config(veille(nom="Rapide", intervalle_secondes=30),
                 veille(nom="Lente", url_recherche="https://www.vinted.fr/catalog?search_text=nike", intervalle_secondes=120))
    banc = Banc(monde, cfg)
    for nom in ("Rapide", "Lente"):
        banc.base.marquer_passage(nom, DEBUT - 1000)
    monde.catalogue = []
    banc.arret_apres = 8
    await banc.moteur.lancer()
    ordre = [p["search_text"] for p in monde.recherches()]
    # la veille à 30 s passe plus souvent que celle à 120 s
    assert ordre.count("carhartt veste") > ordre.count("nike") >= 1


async def test_arret_demande_pendant_l_attente(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = []
    banc.arret_apres = 1
    await banc.moteur.lancer()
    assert len(monde.recherches()) == 1


async def test_variantes_de_marque_cherchent_sans_filtre_de_marque(monde):
    v = veille(nom="Carhartt", url_recherche="https://www.vinted.fr/catalog?search_text=carhartt%20veste&brand_ids[]=362",
               variantes_marque=True)
    banc = Banc(monde, config(v))
    banc.moteur.plans = banc.moteur._construire_plans()
    assert len(banc.moteur.plans) == 4  # recherche principale + 3 variantes (variantes_max_par_veille)
    for plan in banc.moteur.plans:
        banc.base.marquer_passage(plan.cle_etat, DEBUT - 1000)
    monde.catalogue = []
    await banc.moteur.lancer(une_fois=True)
    recherches = monde.recherches()
    assert recherches[0]["search_text"] == "carhartt veste" and recherches[0]["brand_ids"] == "362"
    variantes = recherches[1:]
    assert [p["search_text"] for p in variantes] == ["carhart veste", "carharrt veste", "carthart veste"]
    assert all("brand_ids" not in p for p in variantes)  # sans filtre de marque : c'est tout l'intérêt
    await banc.fermer()


async def test_variantes_sans_marque_connue_ne_cree_rien(monde):
    v = veille(nom="Divers", url_recherche="https://www.vinted.fr/catalog?search_text=veste%20vintage", variantes_marque=True)
    banc = Banc(monde, config(v))
    assert len(banc.moteur._construire_plans()) == 1
    await banc.fermer()


async def test_une_annonce_trouvee_par_variante_n_est_pas_alertee_deux_fois(monde):
    v = veille(nom="Carhartt", url_recherche="https://www.vinted.fr/catalog?search_text=carhartt%20veste", variantes_marque=True)
    banc = Banc(monde, config(v))
    banc.moteur.plans = banc.moteur._construire_plans()
    for plan in banc.moteur.plans:
        banc.base.marquer_passage(plan.cle_etat, DEBUT - 1000)
    monde.catalogue = [monde.item(1, "Carhart veste workwear", prix=25.0, marque="")]  # même annonce dans chaque recherche
    await banc.moteur.lancer(une_fois=True)
    assert len(banc.canal.alertes) == 1  # (domaine, id) déjà vu par la première recherche
    assert "Marque mal orthographiée" in banc.canal.alertes[0].tags[0]
    assert banc.canal.alertes[0].marque.startswith("carhartt")
    await banc.fermer()


async def test_un_bug_sur_une_annonce_n_arrete_pas_la_surveillance(monde, banc, monkeypatch):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0)]

    def boom(*a, **k):
        raise RuntimeError("bug simulé")

    monkeypatch.setattr("bot_vinted.moteur.evaluer", boom)
    await banc.passage()  # ne lève pas
    assert banc.canal.alertes == []
    assert banc.moteur.plans[0].prochaine > monde.t  # le prochain passage est bien planifié


# =============================================================================== réponses inattendues : ralentir, jamais marteler


async def test_catalogue_en_404_declenche_le_backoff_et_une_alerte_sans_marteler(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.scenario = [httpx.Response(404, json={"message": "not found"}) for _ in range(3)]
    banc.arret_apres = 3
    await banc.moteur.lancer()

    # backoff exponentiel appliqué (60 s puis 120 s), et non un nouvel essai à chaque intervalle de 60 s
    assert banc.attentes[:2] == [60.0, 120.0]
    en_pause = [t for t in banc.canal.textes if "en pause" in t]
    assert len(en_pause) == 1 and "réponse inattendue" in en_pause[0] and "404" in en_pause[0]
    assert len(monde.recherches()) == 3  # exactement une requête de catalogue par tentative


async def test_format_du_catalogue_modifie_est_signale(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.scenario = [httpx.Response(200, json={"data": []}), httpx.Response(200, json={"data": []})]
    await banc.passage()
    monde.t += 200
    await banc.passage()
    assert banc.moteur.pause.echecs == 2
    assert any("format" in t and "en pause" in t for t in banc.canal.textes)  # l'utilisateur est prévenu


async def test_details_introuvables_d_affilee_declenchent_la_pause(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(i, prix=25.0) for i in range(1, 6)]  # 5 candidats
    # catalogue OK puis 5 détails en 404 : endpoint modifié ou bloqué, pas 5 ventes simultanées
    monde.scenario = [httpx.Response(200, json={"items": monde.catalogue})] + [httpx.Response(404) for _ in range(5)]
    await banc.passage()
    assert banc.canal.alertes == []
    assert banc.moteur.pause.echecs == 1 and banc.moteur._pause_jusqu_a > monde.t
    assert not banc.base.est_vue("vinted.fr", 5)  # l'annonce qui a déclenché la pause n'est pas perdue


async def test_un_detail_introuvable_isole_ne_declenche_rien(monde, banc):
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0), monde.item(2, prix=24.0)]
    monde.scenario = [httpx.Response(200, json={"items": monde.catalogue}), httpx.Response(404)]  # 1ᵉʳ détail : vendu
    await banc.passage()
    assert [a.annonce_id for a in banc.canal.alertes] == [2]  # le suivant fonctionne normalement
    assert banc.moteur.pause.echecs == 0 and banc.moteur._details_introuvables == 0  # compteur remis à zéro


# =============================================================================== statut publié pour /etat


async def test_statut_du_bot_publie_en_base(monde, banc):
    import json

    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = []
    await banc.passage()
    statut = json.loads(banc.base.lire_meta("statut_bot"))
    assert statut["etat"] == "actif" and statut["maj"] == monde.t and statut["pause_jusqu_a"] is None
    assert statut["motif"] is None and statut["echecs"] == 0

    monde.scenario = [httpx.Response(429)]
    await banc.passage()
    statut = json.loads(banc.base.lire_meta("statut_bot"))
    assert statut["etat"] == "pause" and statut["echecs"] == 1
    assert statut["pause_jusqu_a"] == pytest.approx(monde.t + 60) and "429" in statut["motif"]

    monde.t += 300
    await banc.passage()  # Vinted répond de nouveau
    statut = json.loads(banc.base.lire_meta("statut_bot"))
    assert statut["etat"] == "actif" and statut["motif"] is None and statut["echecs"] == 0
