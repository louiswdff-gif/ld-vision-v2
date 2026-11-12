from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_vinted.vinted.modeles import (
    analyser_annonce,
    analyser_detail,
    analyser_vendeur,
    completer,
)
from bot_vinted.vinted.page_catalogue import analyser_page
from tests.outils import page_html


@pytest.fixture
def catalogue(charger_json):
    """Annonces telles que le bot les lit dans la liste de résultats (la description et la note du vendeur manquent)."""
    return analyser_page(page_html(charger_json("catalogue_liste.json")), "vinted.fr").annonces


def par_id(annonces):
    return {a.id: a for a in annonces}


def test_detail_enrichit_l_annonce(charger_json, catalogue):
    detail = analyser_detail(charger_json("detail_annonce.json"), "vinted.fr")
    assert detail.detail_charge is True
    assert detail.nb_photos == 5
    assert "portée 2 fois" in detail.description
    assert detail.categorie_id == 2050
    assert detail.marque == "Carhartt"
    assert detail.etat == "tres_bon_etat"  # via status_id
    assert detail.publie_le == datetime(2024, 11, 7, 17, 20, tzinfo=timezone.utc).timestamp()

    liste = par_id(catalogue)[10777115810]
    assert liste.publie_le is None and liste.description is None and liste.vendeur.note is None  # ce que la liste ignore
    complete = completer(liste, detail)
    assert complete.detail_charge and complete.nb_photos == 5 and complete.categorie_id == 2050
    assert complete.description and complete.publie_le == detail.publie_le  # comblés par le détail
    assert complete.vendeur.note == pytest.approx(4.8)  # 0.96 × 5
    assert complete.vendeur.nb_avis == 142 and complete.vendeur.pays == "FR"
    assert complete.vendeur.login == "marie_d"
    assert complete.titre == "Carrahrt Detroit jacket marron"  # le titre de la liste n'est pas remplacé


def test_completer_garde_les_valeurs_deja_connues_et_comble_les_manques(catalogue, charger_json):
    detail = analyser_detail(charger_json("detail_annonce.json"), "vinted.fr")
    sans_marque = par_id(catalogue)[10777000003]
    assert sans_marque.marque == ""
    complete = completer(sans_marque, detail)
    assert complete.marque == "Carhartt"  # comblée depuis le détail
    assert complete.titre == "Veste Carhart workwear"  # le reste est inchangé


def test_vendeur_note_et_avis():
    v = analyser_vendeur({"id": 1, "login": "x", "feedback_reputation": 0.9, "feedback_count": 10, "country_iso_code": "be"})
    assert v.note == 4.5 and v.nb_avis == 10 and v.pays == "BE"


def test_vendeur_note_deja_sur_5_et_comptes_detailles():
    v = analyser_vendeur({"feedback_reputation": 4.7, "positive_feedback_count": 8, "neutral_feedback_count": 1,
                          "negative_feedback_count": 1})
    assert v.note == 4.7 and v.nb_avis == 10


def test_vendeur_sans_avis_n_a_pas_de_note():
    v = analyser_vendeur({"id": 2, "login": "neuf", "feedback_reputation": 0.0, "feedback_count": 0})
    assert v.note is None and v.nb_avis == 0


def test_vendeur_absent():
    assert analyser_vendeur(None) is None and analyser_vendeur({}) is None


@pytest.mark.parametrize("etat_json, attendu", [
    ({"status": "New with tags"}, "neuf_avec_etiquette"),
    ({"status": "Very good"}, "tres_bon_etat"),
    ({"status": "Nuevo sin etiquetas"}, "neuf_sans_etiquette"),
    ({"status": "Satisfaisant"}, "satisfaisant"),
    ({"status_id": 6, "status": "n'importe quoi"}, "neuf_avec_etiquette"),  # l'identifiant prime
    ({"status": {"id": 3, "title": "Bon état"}}, "bon_etat"),
])
def test_etats_dans_plusieurs_langues_et_formes(etat_json, attendu):
    a = analyser_annonce({"id": 1, "title": "t", "price": "10", **etat_json}, "vinted.fr")
    assert a.etat == attendu


def test_horodatage_en_millisecondes_et_iso():
    a = analyser_annonce({"id": 1, "title": "t", "price": "1", "created_at_ts": 1731000000000}, "vinted.fr")
    assert a.publie_le == 1731000000
    b = analyser_annonce({"id": 1, "title": "t", "price": "1", "created_at_ts": "2024-11-07T17:20:00Z"}, "vinted.fr")
    assert b.publie_le == 1731000000


def test_annonce_invisible():
    a = analyser_annonce({"id": 1, "title": "t", "price": "1", "is_visible": False}, "vinted.fr")
    assert a.visible is False


def test_annonce_illisible_leve_valueerror():
    with pytest.raises(ValueError):
        analyser_annonce({"title": "x", "price": "1"}, "vinted.fr")
    with pytest.raises(ValueError):
        analyser_annonce({"id": 1, "title": "x"}, "vinted.fr")
