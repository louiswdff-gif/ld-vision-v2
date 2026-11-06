from __future__ import annotations

import pytest

from bot_vinted.base import Base
from bot_vinted.config import FraisAcheteur, Revente, Veille
from bot_vinted.valorisation import (
    JOUR,
    CleHistorique,
    categorie_de,
    categorie_par_defaut,
    cle_historique,
    cout_reel,
    estimer,
)
from bot_vinted.vinted.modeles import Annonce

MAINTENANT = 1_731_000_000.0
CLE = CleHistorique("carhartt", "2050", "l", "tres_bon_etat")


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


def annonce(**kw) -> Annonce:
    d = dict(domaine="vinted.fr", id=1, titre="Veste Carhartt", url="u", prix=28.0, marque="Carhartt", taille="L",
             etat="tres_bon_etat", categorie_id=2050)
    d.update(kw)
    return Annonce(**d)


def veille(**kw) -> Veille:
    return Veille(**{"nom": "T", "mots_cles": ["carhartt"], "port_estime": 6.5, "prix_revente_cible": 70, **kw})


def remplir(base: Base, prix: list[float], cle: CleHistorique = CLE, depuis: float = MAINTENANT - 3600) -> None:
    for i, p in enumerate(prix):
        base.enregistrer_prix("vinted.fr", 1000 + i, cle.marque, cle.categorie, cle.taille, cle.etat, p, depuis)


# ------------------------------------------------------------------ coût réel


def test_cout_reel_selon_le_cahier_des_charges():
    # 28 € + 0,70 € + 5 % de 28 (1,40 €) + 6,50 € de port = 36,60 €
    assert cout_reel(28.0, FraisAcheteur(), 6.5) == 36.60
    assert cout_reel(100.0, FraisAcheteur(fixe=0.70, pourcentage=5), 0) == 105.70


def test_frais_parametrables():
    assert cout_reel(20.0, FraisAcheteur(fixe=1.0, pourcentage=10), 4.0) == 27.0
    assert cout_reel(20.0, FraisAcheteur(fixe=0, pourcentage=0), 0) == 20.0


# ------------------------------------------------------------------ prix de revente


def test_moins_de_10_references_utilise_le_prix_cible(base):
    remplir(base, [100.0] * 9)  # 9 < 10 : insuffisant
    v = estimer(annonce(), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.source_revente == "cible" and v.revente_estimee == 70.0
    assert v.nb_references == 9 and v.mediane is None


def test_10_references_ou_plus_utilise_mediane_et_decote(base):
    remplir(base, [60, 70, 80, 90, 100, 50, 55, 65, 75, 85])  # médiane = (70+75)/2 = 72.5
    v = estimer(annonce(), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.source_revente == "mediane" and v.nb_references == 10
    assert v.mediane == 72.5
    assert v.revente_estimee == pytest.approx(72.5 * 0.85, abs=0.01)  # 61,63 €


def test_decote_et_seuil_configurables(base):
    remplir(base, [100.0] * 5)
    v = estimer(annonce(), CLE, veille(), FraisAcheteur(), Revente(min_references=5, decote=0.5), base, MAINTENANT)
    assert v.source_revente == "mediane" and v.revente_estimee == 50.0


def test_marge_roi_et_ecart(base):
    v = estimer(annonce(prix=28.0), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.cout_reel == 36.60
    assert v.marge == 33.40  # 70 − 36,60
    assert v.roi_pct == 91.3  # 33,40 / 36,60
    assert v.ecart_pct == 60.0  # (70 − 28) / 70


def test_marge_negative_possible(base):
    v = estimer(annonce(prix=60.0), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.marge < 0 and v.roi_pct < 0 and v.ecart_pct == pytest.approx(14.3, abs=0.1)


def test_aucune_estimation_possible_sans_references_ni_cible(base):
    assert estimer(annonce(), CLE, veille(prix_revente_cible=None), FraisAcheteur(), Revente(), base, MAINTENANT) is None


def test_l_annonce_evaluee_n_est_pas_sa_propre_reference(base):
    remplir(base, [100.0] * 9)
    base.enregistrer_prix("vinted.fr", 1, CLE.marque, CLE.categorie, CLE.taille, CLE.etat, 28.0, MAINTENANT)  # elle-même
    v = estimer(annonce(id=1), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.nb_references == 9 and v.source_revente == "cible"  # elle ne compte pas : 10 → 9


def test_references_trop_anciennes_ignorees(base):
    remplir(base, [100.0] * 10, depuis=MAINTENANT - 61 * JOUR)  # hors fenêtre de 60 jours
    v = estimer(annonce(), CLE, veille(), FraisAcheteur(), Revente(fenetre_jours=60), base, MAINTENANT)
    assert v.source_revente == "cible" and v.nb_references == 0


def test_references_d_une_autre_combinaison_ignorees(base):
    remplir(base, [100.0] * 10, cle=CleHistorique("carhartt", "2050", "m", "tres_bon_etat"))  # autre taille
    v = estimer(annonce(), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.source_revente == "cible"


def test_port_estime_absent_vaut_zero(base):
    v = estimer(annonce(prix=28.0), CLE, veille(port_estime=None), FraisAcheteur(), Revente(), base, MAINTENANT)
    assert v.cout_reel == 30.10


# ------------------------------------------------------------------ lots


def test_lot_valeur_unitaire_fois_quantite_avec_decote_de_lot(base):
    # lot de 3 à 45 € ; cible 70 €/pièce → 3 × 70 × 0,90 = 189 €
    v = estimer(annonce(prix=45.0), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT, quantite=3)
    assert v.quantite == 3 and v.revente_estimee == 189.0
    assert v.cout_reel == 54.45  # 45 + 0,70 + 2,25 + 6,50
    assert v.marge == pytest.approx(134.55)
    assert v.ecart_pct == pytest.approx((70 - 15) / 70 * 100, abs=0.1)  # prix unitaire 15 € vs 70 €


def test_lot_avec_mediane(base):
    remplir(base, [40.0] * 10)
    v = estimer(annonce(prix=30.0), CLE, veille(), FraisAcheteur(), Revente(), base, MAINTENANT, quantite=2)
    assert v.revente_estimee == pytest.approx(40 * 0.85 * 2 * 0.90, abs=0.01)  # 61,20


# ------------------------------------------------------------------ clé d'historique


def test_cle_historique_normalisee():
    a = annonce(marque="Carhartt", taille=" XL ", etat="bon_etat", categorie_id=2050)
    assert cle_historique(a, "carhartt", "2050") == CleHistorique("carhartt", "2050", "xl", "bon_etat")


def test_cle_historique_valeurs_de_repli():
    a = annonce(taille="", etat=None, categorie_id=None)
    assert cle_historique(a, "", "2050-1776") == CleHistorique("inconnue", "2050-1776", "?", "inconnu")
    assert cle_historique(a, "x", "").categorie == "0"


def test_categorie_de_l_annonce_ou_de_la_recherche():
    assert categorie_de(annonce(categorie_id=2050), [1776]) == "2050"  # l'annonce indique la sienne
    assert categorie_de(annonce(categorie_id=None), [1776, 2050]) == "1776-2050"  # repli : la recherche
    assert categorie_de(annonce(categorie_id=None), []) == "0"


def test_categorie_par_defaut():
    assert categorie_par_defaut([1776, 2050]) == "1776-2050"
    assert categorie_par_defaut([]) == "0"
