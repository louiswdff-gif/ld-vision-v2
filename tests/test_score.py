from __future__ import annotations

import pytest

from bot_vinted.config import Score
from bot_vinted.score import NEUTRE, calculer_score, palier, score_vendeur
from bot_vinted.valorisation import Valorisation
from bot_vinted.vinted.modeles import Annonce, Vendeur


def valo(**kw) -> Valorisation:
    d = dict(prix=28.0, cout_reel=36.6, revente_estimee=70.0, source_revente="cible", nb_references=0, mediane=None,
             marge=33.4, roi_pct=91.3, ecart_pct=60.0, quantite=1)
    d.update(kw)
    return Valorisation(**d)


def annonce(**kw) -> Annonce:
    d = dict(domaine="vinted.fr", id=1, titre="t", url="u", prix=28.0, etat="neuf_avec_etiquette",
             vendeur=Vendeur(note=5.0, nb_avis=50))
    d.update(kw)
    return Annonce(**d)


def test_annonce_excellente():
    r = calculer_score(valo(), annonce(), age_minutes=0, bonus_pepites=0, cfg=Score())
    # écart 100 · marge 83,5 · roi 91,3 · état 100 · fraîcheur 100 · vendeur 100 → 94,135
    assert r.criteres == {"ecart_mediane": 100.0, "marge": 83.5, "roi": 91.3, "etat": 100.0, "fraicheur": 100.0,
                          "vendeur": 100.0}
    assert r.total == 94 and r.palier == "🔥" and r.bonus == 0


def test_annonce_mediocre():
    v = valo(ecart_pct=0.0, marge=5.0, roi_pct=5.0)
    r = calculer_score(v, annonce(etat="satisfaisant", vendeur=None), age_minutes=60, bonus_pepites=0, cfg=Score())
    # 0·30 + 12,5·25 + 5·20 + 20·10 + 1,5625·10 + 50·5 = 878,125 → 8,78
    assert r.total == 9 and r.palier == "🔹"


def test_ecart_negatif_ou_marge_negative_bornes_a_zero():
    r = calculer_score(valo(ecart_pct=-30, marge=-10, roi_pct=-20), annonce(), 0, 0, Score())
    assert r.criteres["ecart_mediane"] == 0 and r.criteres["marge"] == 0 and r.criteres["roi"] == 0


def test_bonus_pepites_plafonne():
    v = valo(ecart_pct=0.0, marge=5.0, roi_pct=5.0)
    a = annonce(etat="satisfaisant", vendeur=None)
    sans = calculer_score(v, a, 60, 0, Score()).total  # 9
    avec_8 = calculer_score(v, a, 60, 8, Score())
    assert avec_8.total == 17 and avec_8.bonus == 8  # 8,78 + 8 = 16,78
    plafonne = calculer_score(v, a, 60, 50, Score())  # plafond de 20 par défaut
    assert plafonne.bonus == 20 and plafonne.total == 29 and sans == 9


def test_score_borne_a_100():
    r = calculer_score(valo(), annonce(), 0, 20, Score())
    assert r.total == 100


def test_paliers_cahier_des_charges():
    cfg = Score()
    assert palier(100, cfg) == "🔥" and palier(80, cfg) == "🔥"
    assert palier(79, cfg) == "✅" and palier(60, cfg) == "✅"
    assert palier(59, cfg) == "🔹" and palier(0, cfg) == "🔹"


def test_paliers_configurables():
    cfg = Score(seuils={"feu": 90, "ok": 50})
    assert palier(85, cfg) == "✅" and palier(90, cfg) == "🔥" and palier(49, cfg) == "🔹"


def test_fraicheur_demi_vie():
    cfg = Score(fraicheur_demi_vie_minutes=10)
    f = lambda age: calculer_score(valo(), annonce(), age, 0, cfg).criteres["fraicheur"]
    assert f(0) == 100.0 and f(10) == 50.0 and f(20) == 25.0
    assert f(None) == NEUTRE  # date inconnue : neutre


def test_etat_selon_la_config():
    cfg = Score()
    e = lambda etat: calculer_score(valo(), annonce(etat=etat), 0, 0, cfg).criteres["etat"]
    assert [e(x) for x in ("neuf_avec_etiquette", "neuf_sans_etiquette", "tres_bon_etat", "bon_etat", "satisfaisant")] == \
           [100, 85, 70, 45, 20]
    assert e(None) == NEUTRE  # état inconnu


def test_poids_configurables():
    seulement_marge = Score(poids={"ecart_mediane": 0, "marge": 1, "roi": 0, "etat": 0, "fraicheur": 0, "vendeur": 0})
    r = calculer_score(valo(marge=20.0), annonce(), 0, 0, seulement_marge)
    assert r.total == 50  # 20 / 40 € de marge pleine


def test_tous_les_poids_a_zero_ne_plante_pas():
    nul = Score(poids={"ecart_mediane": 0, "marge": 0, "roi": 0, "etat": 0, "fraicheur": 0, "vendeur": 0})
    assert calculer_score(valo(), annonce(), 0, 0, nul).total == 0


def test_score_vendeur():
    assert score_vendeur(None, 20) == NEUTRE
    assert score_vendeur(Vendeur(note=None, nb_avis=0), 20) == NEUTRE
    assert score_vendeur(Vendeur(note=5.0, nb_avis=0), 20) == NEUTRE  # aucun avis : on ne se fie pas à la note
    assert score_vendeur(Vendeur(note=4.0, nb_avis=10), 20) == pytest.approx(65.0)  # moitié de confiance
    assert score_vendeur(Vendeur(note=4.0, nb_avis=500), 20) == pytest.approx(80.0)  # pleine confiance
    assert score_vendeur(Vendeur(note=2.0, nb_avis=500), 20) == pytest.approx(40.0)
