from __future__ import annotations

import pathlib

import pytest

from bot_vinted.base import Base
from bot_vinted.cli import principal
from bot_vinted.suivi import (
    JOUR,
    SANS_VEILLE,
    ErreurSuivi,
    acheter,
    calculer_stats,
    extraire_reference,
    formater_stats,
    vendre,
)

T0 = 1_731_000_000.0


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


def annonce_vue(base: Base, annonce_id: int = 4001, domaine: str = "vinted.fr", veille: str = "Carhartt vestes",
                marge: float = 33.4) -> None:
    base.enregistrer_annonce(dict(
        domaine=domaine, annonce_id=annonce_id, veille=veille, titre="Veste Carhartt Detroit", marque="Carhartt",
        taille="L", etat="tres_bon_etat", url="u", photo_url=None, prix=28.0, cout_reel=36.6, revente_estimee=70.0,
        source_revente="cible", nb_references=0, marge=marge, roi=91.3, score=80, tags="", publie_le=T0,
        vendeur_note=4.8, vendeur_avis=10, alerte_statut="envoyee", alerte_le=T0, cree_le=T0,
    ))


# ------------------------------------------------------------------ référence d'annonce


def test_extraire_reference():
    assert extraire_reference("4001") == (4001, None)
    assert extraire_reference(" 4001 ") == (4001, None)
    assert extraire_reference("https://www.vinted.fr/items/4001-veste-carhartt") == (4001, "vinted.fr")
    assert extraire_reference("https://www.vinted.be/items/77-pull?referrer=catalog") == (77, "vinted.be")
    assert extraire_reference("www.vinted.fr/items/12") == (12, None)  # sans schéma : domaine non déduit
    with pytest.raises(ErreurSuivi, match="ni un identifiant"):
        extraire_reference("pas-une-annonce")


# ------------------------------------------------------------------ acheter / vendu


def test_acheter_annonce_connue_reprend_veille_titre_et_marge_estimee(base):
    annonce_vue(base)
    msg = acheter(base, "4001", 36.1, T0, domaine_defaut="vinted.fr")
    assert "enregistré" in msg and "36.10 €" in msg and "Carhartt vestes" in msg and "+33.40 €" in msg
    achat = base.achat("vinted.fr", 4001)
    assert (achat["veille"], achat["titre"], achat["marge_estimee"], achat["prix_achat_total"]) == \
           ("Carhartt vestes", "Veste Carhartt Detroit", 33.4, 36.1)


def test_acheter_annonce_inconnue_du_bot(base):
    msg = acheter(base, "999", 50.0, T0, domaine_defaut="vinted.fr", titre="Pull trouvé à la main", veille="Manuel")
    assert "jamais vue par le bot" in msg
    achat = base.achat("vinted.fr", 999)
    assert (achat["veille"], achat["titre"], achat["marge_estimee"]) == ("Manuel", "Pull trouvé à la main", None)


def test_acheter_par_url_deduit_le_domaine(base):
    acheter(base, "https://www.vinted.be/items/55-pull", 20.0, T0, domaine_defaut="vinted.fr")
    assert base.achat("vinted.be", 55) is not None and base.achat("vinted.fr", 55) is None


def test_acheter_domaine_ambigu_demande_une_precision(base):
    annonce_vue(base, domaine="vinted.fr")
    annonce_vue(base, domaine="vinted.be")
    with pytest.raises(ErreurSuivi, match="--domaine"):
        acheter(base, "4001", 30.0, T0, domaine_defaut="vinted.fr")
    acheter(base, "4001", 30.0, T0, domaine_defaut="vinted.fr", domaine="vinted.be")
    assert base.achat("vinted.be", 4001) is not None


def test_reacheter_corrige_le_prix(base):
    acheter(base, "1", 30.0, T0, domaine_defaut="vinted.fr")
    msg = acheter(base, "1", 32.0, T0 + 100, domaine_defaut="vinted.fr")
    assert "corrigé" in msg and base.achat("vinted.fr", 1)["prix_achat_total"] == 32.0


def test_vendu_calcule_marge_roi_delai_et_ecart(base):
    annonce_vue(base)
    acheter(base, "4001", 40.0, T0, domaine_defaut="vinted.fr")
    msg = vendre(base, "4001", 70.0, T0 + 5 * JOUR, domaine_defaut="vinted.fr")
    assert "+30.00 €" in msg and "+75 %" in msg and "5.0 jour" in msg
    assert "+33.40 €" in msg and "-3.40 €" in msg  # marge estimée 33,40 € vs réelle 30 € → écart −3,40 €
    achat = base.achat("vinted.fr", 4001)
    assert achat["prix_vente_net"] == 70.0 and achat["date_vente"] == T0 + 5 * JOUR


def test_vendu_sans_achat_et_double_vente(base):
    with pytest.raises(ErreurSuivi, match="Aucun achat"):
        vendre(base, "123", 50.0, T0, domaine_defaut="vinted.fr")
    acheter(base, "123", 30.0, T0, domaine_defaut="vinted.fr")
    vendre(base, "123", 50.0, T0 + JOUR, domaine_defaut="vinted.fr")
    with pytest.raises(ValueError, match="déjà"):  # un article vendu ne peut plus être « racheté »
        acheter(base, "123", 31.0, T0 + 2 * JOUR, domaine_defaut="vinted.fr")


# ------------------------------------------------------------------ statistiques


def remplir(base: Base) -> None:
    annonce_vue(base, 1, veille="Carhartt vestes", marge=30.0)
    annonce_vue(base, 2, veille="Carhartt vestes", marge=20.0)
    annonce_vue(base, 3, veille="Ralph Lauren pulls", marge=15.0)
    acheter(base, "1", 40.0, T0, domaine_defaut="vinted.fr")                  # vendu 70  : +30 (75 %), 5 j
    acheter(base, "2", 30.0, T0, domaine_defaut="vinted.fr")                  # vendu 40  : +10 (33 %), 15 j
    acheter(base, "3", 20.0, T0, domaine_defaut="vinted.fr")                  # vendu 18  : −2  (−10 %), 10 j
    acheter(base, "4", 25.0, T0, domaine_defaut="vinted.fr")                  # en stock, jamais vu par le bot
    vendre(base, "1", 70.0, T0 + 5 * JOUR, domaine_defaut="vinted.fr")
    vendre(base, "2", 40.0, T0 + 15 * JOUR, domaine_defaut="vinted.fr")
    vendre(base, "3", 18.0, T0 + 10 * JOUR, domaine_defaut="vinted.fr")


def test_calcul_des_statistiques(base):
    remplir(base)
    s = calculer_stats(base.tous_les_achats())
    assert (s.achats, s.vendus, s.en_stock) == (4, 3, 1)
    assert s.capital_immobilise == 25.0
    assert s.marge_totale == pytest.approx(38.0)  # 30 + 10 − 2
    assert s.marge_moyenne == pytest.approx(38 / 3)
    assert s.roi_moyen == pytest.approx((75 + 33.333333 - 10) / 3, abs=0.01)
    assert s.roi_global == pytest.approx(38 / 90 * 100)  # marge totale / total acheté (40 + 30 + 20)
    assert s.delai_moyen_jours == pytest.approx(10.0)  # (5 + 15 + 10) / 3
    # estimé (30, 20, 15 → moyenne 21,67) vs réel des mêmes articles (30, 10, −2 → moyenne 12,67)
    assert s.nb_comparables == 3
    assert s.marge_estimee_moyenne == pytest.approx(65 / 3)
    assert s.marge_reelle_moyenne_comparable == pytest.approx(38 / 3)


def test_veilles_classees_par_rentabilite(base):
    remplir(base)
    s = calculer_stats(base.tous_les_achats())
    assert [v.veille for v in s.par_veille] == ["Carhartt vestes", "Ralph Lauren pulls", SANS_VEILLE]
    carhartt = s.par_veille[0]
    assert (carhartt.vendus, carhartt.marge_totale) == (2, pytest.approx(40.0))
    assert carhartt.roi_moyen == pytest.approx((75 + 33.333333) / 2, abs=0.01)
    assert carhartt.delai_moyen == pytest.approx(10.0)
    assert s.par_veille[2].achats == 1 and s.par_veille[2].vendus == 0  # l'article en stock sans veille


def test_affichage_des_statistiques(base):
    remplir(base)
    texte = formater_stats(calculer_stats(base.tous_les_achats()))
    for attendu in ("Achats : 4 (3 revendu(s), 1 en stock", "capital immobilisé 25.00 €", "+38.00 €", "ROI moyen",
                    "délai moyen de revente : 10.0 j", "1. Carhartt vestes", "2. Ralph Lauren pulls", "3. (sans veille)",
                    "SURESTIME"):  # estimé 21,67 vs réel 12,67 : −42 % → conseil de prudence
        assert attendu in texte, attendu


def test_conseil_sous_estimation_et_coherence(base):
    annonce_vue(base, 1, marge=10.0)
    acheter(base, "1", 40.0, T0, domaine_defaut="vinted.fr")
    vendre(base, "1", 70.0, T0 + JOUR, domaine_defaut="vinted.fr")  # réel +30 vs estimé +10
    assert "SOUS-ESTIME" in formater_stats(calculer_stats(base.tous_les_achats()))

    annonce_vue(base, 2, marge=30.0)
    acheter(base, "2", 40.0, T0, domaine_defaut="vinted.fr")
    vendre(base, "2", 70.0, T0 + JOUR, domaine_defaut="vinted.fr")  # réel +30 vs estimé +30
    texte = formater_stats(calculer_stats(base.tous_les_achats()))  # moyennes : estimé 20, réel 30 → +50 % : sous-estime
    assert "SOUS-ESTIME" in texte


def test_stats_sans_achat():
    texte = formater_stats(calculer_stats([]))
    assert "Aucun achat" in texte and "acheter" in texte


def test_stats_avec_achats_non_vendus_seulement(base):
    acheter(base, "1", 40.0, T0, domaine_defaut="vinted.fr")
    s = calculer_stats(base.tous_les_achats())
    assert s.vendus == 0 and s.marge_moyenne is None and s.roi_moyen is None
    texte = formater_stats(s)
    assert "1 en stock" in texte and "—" in texte  # pas de division par zéro


# ------------------------------------------------------------------ CLI


@pytest.fixture
def config_cli(tmp_path):
    chemin = tmp_path / "config.yaml"
    chemin.write_text("base: {chemin: donnees/suivi.db}\n", encoding="utf-8")
    return str(chemin)


def test_cli_acheter_vendu_stats(config_cli, capsys):
    assert principal(["stats", "--config", config_cli]) == 0
    assert "Aucun achat" in capsys.readouterr().out
    assert not (pathlib.Path(config_cli).parent / "donnees").exists()  # `stats` ne crée pas de base vide

    assert principal(["acheter", "4001", "36,10", "--titre", "Veste", "--veille", "Manuel", "--config", config_cli]) == 0
    assert "36.10 €" in capsys.readouterr().out
    assert principal(["vendu", "https://www.vinted.fr/items/4001-veste", "70", "--config", config_cli]) == 0
    assert "+33.90 €" in capsys.readouterr().out
    assert principal(["stats", "--config", config_cli]) == 0
    sortie = capsys.readouterr().out
    assert "Manuel" in sortie and "+33.90 €" in sortie


def test_cli_erreurs_d_usage(config_cli, capsys):
    assert principal(["vendu", "123", "50", "--config", config_cli]) == 1
    assert "Aucun achat" in capsys.readouterr().err
    assert principal(["acheter", "n'importe quoi", "50", "--config", config_cli]) == 1
    assert "identifiant" in capsys.readouterr().err
    assert principal(["acheter", "5", "10", "--domaine", "exemple.com", "--config", config_cli]) == 1
    assert "domaine" in capsys.readouterr().err.lower()
