from __future__ import annotations

import pytest

from bot_vinted.base import Base
from bot_vinted.config import Config, Veille
from bot_vinted.evaluation import evaluer
from bot_vinted.marques import CatalogueMarques
from bot_vinted.vinted.modeles import Annonce, Vendeur

from tests.conftest import RACINE_PROJET

MAINTENANT = 1_731_000_000.0


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config()


@pytest.fixture(scope="module")
def marques(cfg) -> CatalogueMarques:
    return CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", cfg.marques)


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


def annonce(**kw) -> Annonce:
    d = dict(domaine="vinted.fr", id=1, titre="Veste Carhartt Detroit", url="u", prix=28.0, marque="Carhartt",
             taille="L", etat="tres_bon_etat", publie_le=MAINTENANT - 120, categorie_id=2050,
             vendeur=Vendeur(note=4.8, nb_avis=142, pays="FR"))
    d.update(kw)
    return Annonce(**d)


def veille(**kw) -> Veille:
    return Veille(**{"nom": "T", "mots_cles": ["carhartt"], "port_estime": 6.5, "prix_revente_cible": 70, **kw})


def evalue(a, base, cfg, marques, v=None):
    return evaluer(a, v or veille(), "2050", cfg, base, marques, MAINTENANT)


def test_bonne_affaire_sans_signal(base, cfg, marques):
    e = evalue(annonce(), base, cfg, marques)
    assert e.valorisation.marge == 33.40 and e.valorisation.source_revente == "cible"
    assert e.signaux == [] and e.tags == []
    assert e.age_minutes == pytest.approx(2.0)
    assert e.score.palier in ("🔥", "✅") and 70 <= e.score.total <= 100
    assert (e.cle.marque, e.cle.categorie, e.cle.taille, e.cle.etat) == ("carhartt", "2050", "l", "tres_bon_etat")


def test_pas_estimable_sans_references_ni_cible(base, cfg, marques):
    assert evalue(annonce(), base, cfg, marques, veille(prix_revente_cible=None)) is None


def test_pepite_marque_mal_orthographiee_absente_du_champ(base, cfg, marques):
    pepite = annonce(titre="Carhart veste workwear", marque="")
    normale = annonce(titre="Veste Carhartt workwear")
    e = evalue(pepite, base, cfg, marques)
    n = evalue(normale, base, cfg, marques)
    assert "marque_mal_orthographiee" in e.signaux
    assert e.marque.marque == "carhartt"  # la marque effective sert de clé d'historique
    assert e.cle.marque == "carhartt"
    assert any("Marque mal orthographiée" in t and "carhart" in t for t in e.tags)
    assert e.score.bonus == cfg.score.bonus.marque_mal_orthographiee
    assert e.score.total > n.score.total or e.score.total == 100


def test_marque_correcte_mais_absente_du_champ(base, cfg, marques):
    e = evalue(annonce(titre="Veste Carhartt Detroit", marque=""), base, cfg, marques)
    assert e.signaux == ["marque_absente_du_champ"] and e.tags == ["🏷️ Marque absente du champ (carhartt)"]
    assert e.score.bonus == cfg.score.bonus.marque_absente_du_champ


def test_marque_trouvee_dans_la_description_apres_detail(base, cfg, marques):
    sans_detail = annonce(titre="Veste workwear marron", marque="")
    assert evalue(sans_detail, base, cfg, marques).marque.marque == "inconnue"
    avec_detail = sans_detail.model_copy(update={"description": "Magnifique veste carharrt, jamais portée"})
    e = evalue(avec_detail, base, cfg, marques)
    assert e.marque.marque == "carhartt" and "marque_mal_orthographiee" in e.signaux


def test_vente_pressee(base, cfg, marques):
    e = evalue(annonce(titre="Veste Carhartt URGENT déménagement"), base, cfg, marques)
    assert e.signaux == ["vente_pressee"] and "urgent" in e.tags[0]
    # le signal peut aussi venir de la description chargée plus tard
    e2 = evalue(annonce().model_copy(update={"description": "Vide dressing, tout doit partir"}), base, cfg, marques)
    assert "vente_pressee" in e2.signaux


def test_annonce_mal_valorisee(base, cfg, marques):
    e = evalue(annonce(titre="Veste", nb_photos=1, marque=""), base, cfg, marques)
    assert "annonce_mal_valorisee" in e.signaux
    assert any("titre vague" in t and "1 photo" in t for t in e.tags)


def test_lot_avec_quantite_evalue_a_l_unite(base, cfg, marques):
    e = evalue(annonce(titre="Lot de 3 pulls Ralph Lauren", marque="Ralph Lauren", prix=45.0), base, cfg, marques)
    assert e.valorisation.quantite == 3
    assert e.valorisation.revente_estimee == 189.0  # 3 × 70 € × 0,90
    assert "lot" in e.signaux and e.tags == ["📦 Lot de 3 (valeur estimée à l'unité)"]


def test_lot_sans_quantite_tague_mais_sans_bonus(base, cfg, marques):
    e = evalue(annonce(titre="Lot de vêtements Carhartt"), base, cfg, marques)
    assert e.valorisation.quantite == 1 and "lot" not in e.signaux
    assert any("quantité inconnue" in t for t in e.tags) and e.score.bonus == 0


def test_cumul_de_signaux_plafonne(base, cfg, marques):
    a = annonce(titre="Carhart URGENT", marque="", nb_photos=1)  # 3 signaux : 10 + 8 (titre court) + 5 = 23 → plafond 20
    e = evalue(a, base, cfg, marques)
    assert set(e.signaux) == {"marque_mal_orthographiee", "annonce_mal_valorisee", "vente_pressee"}
    assert e.score.bonus == cfg.score.bonus.plafond == 20


def test_mediane_utilisee_quand_assez_de_references(base, cfg, marques):
    for i in range(12):
        base.enregistrer_prix("vinted.fr", 100 + i, "carhartt", "2050", "l", "tres_bon_etat", 60.0, MAINTENANT - 600)
    e = evalue(annonce(), base, cfg, marques)
    assert e.valorisation.source_revente == "mediane" and e.valorisation.mediane == 60.0
    assert e.valorisation.revente_estimee == 51.0  # 60 × 0,85
    assert e.valorisation.marge == pytest.approx(14.4)  # 51 − 36,60


def test_age_inconnu(base, cfg, marques):
    e = evalue(annonce(publie_le=None), base, cfg, marques)
    assert e.age_minutes is None and e.score.criteres["fraicheur"] == 50.0
