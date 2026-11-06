from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_vinted.vinted.modeles import (
    analyser_annonce,
    analyser_catalogue,
    analyser_detail,
    analyser_vendeur,
    completer,
)


@pytest.fixture
def catalogue(charger_json):
    return analyser_catalogue(charger_json("catalogue.json"), "vinted.fr")


def par_id(annonces):
    return {a.id: a for a in annonces}


def test_catalogue_ignore_les_elements_illisibles(catalogue):
    # 8 éléments dans la fixture : un sans prix et un sans identifiant sont écartés
    assert [a.id for a in catalogue] == [4001, 4002, 4003, 4004, 4005, 4006]


def test_annonce_complete_du_catalogue(catalogue):
    a = par_id(catalogue)[4001]
    assert a.titre == "Veste Carhartt Detroit"
    assert a.prix == 28.0 and a.devise == "EUR"
    assert a.marque == "Carhartt" and a.taille == "L"
    assert a.etat == "tres_bon_etat" and a.etat_libelle == "Très bon état"
    assert a.publie_le == 1731000000  # timestamp de la photo principale
    assert a.photo_url.endswith("4001.jpeg?s=abc")
    assert a.url == "https://www.vinted.fr/items/4001-veste-carhartt-detroit"
    assert a.vendeur.login == "marie_d" and a.vendeur.note is None  # note inconnue dans le catalogue
    assert a.favoris == 3 and a.vues == 12
    # Le catalogue ne donne ni description ni nombre de photos : détail à charger plus tard
    assert a.description is None and a.nb_photos is None and a.detail_charge is False


def test_prix_en_chaine_et_devise_separee(catalogue):
    a = par_id(catalogue)[4002]
    assert a.prix == 22.5 and a.devise == "EUR"
    assert a.marque == ""  # marque absente du champ
    assert a.etat == "neuf_sans_etiquette"


def test_prix_avec_virgule_decimale(catalogue):
    assert par_id(catalogue)[4006].prix == 30.5


def test_etat_inconnu_est_none_mais_conserve_le_libelle(catalogue):
    a = par_id(catalogue)[4006]
    assert a.etat is None and a.etat_libelle == "Etat mystérieux"


def test_annonce_sans_photo_ni_date(catalogue):
    a = par_id(catalogue)[4004]
    assert a.photo_url is None and a.publie_le is None
    assert a.url == "https://www.vinted.fr/items/4004-veste"  # reconstruite depuis `path`
    assert a.age_minutes(1731000000) is None


def test_vendeur_professionnel(catalogue):
    assert par_id(catalogue)[4003].vendeur.pro is True


def test_age_en_minutes(catalogue):
    a = par_id(catalogue)[4001]
    assert a.age_minutes(1731000000 + 600) == pytest.approx(10.0)
    assert a.age_minutes(1731000000 - 50) == 0.0  # jamais négatif


def test_detail_enrichit_l_annonce(charger_json, catalogue):
    detail = analyser_detail(charger_json("detail_annonce.json"), "vinted.fr")
    assert detail.detail_charge is True
    assert detail.nb_photos == 5
    assert "portée 2 fois" in detail.description
    assert detail.categorie_id == 2050
    assert detail.marque == "Carhartt"
    assert detail.etat == "tres_bon_etat"  # via status_id
    assert detail.publie_le == datetime(2024, 11, 7, 17, 20, tzinfo=timezone.utc).timestamp()

    complete = completer(par_id(catalogue)[4001], detail)
    assert complete.detail_charge and complete.nb_photos == 5 and complete.categorie_id == 2050
    assert complete.publie_le == 1731000000  # la date du catalogue n'est pas écrasée
    assert complete.vendeur.note == pytest.approx(4.8)  # 0.96 × 5
    assert complete.vendeur.nb_avis == 142 and complete.vendeur.pays == "FR"
    assert complete.vendeur.login == "marie_d"


def test_completer_garde_les_valeurs_deja_connues_et_comble_les_manques(catalogue, charger_json):
    detail = analyser_detail(charger_json("detail_annonce.json"), "vinted.fr")
    sans_marque = par_id(catalogue)[4002]
    complete = completer(sans_marque, detail)
    assert complete.marque == "Carhartt"  # comblée depuis le détail
    assert complete.titre == "Carhart veste workwear"  # le reste est inchangé


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
