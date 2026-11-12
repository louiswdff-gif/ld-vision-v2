from __future__ import annotations

import json

import pytest

from bot_vinted.vinted.page_catalogue import (
    PageInattendue,
    analyser_entree,
    analyser_page,
    extraire_flux,
    extraire_liste,
)

from tests.outils import page_html


@pytest.fixture
def liste(charger_json) -> dict:
    return charger_json("catalogue_liste.json")


@pytest.fixture
def page(liste):
    return analyser_page(page_html(liste), "vinted.fr")


def par_id(annonces):
    return {a.id: a for a in annonces}


# ------------------------------------------------------------------ extraction depuis le HTML


def test_flux_reconstitue_malgre_le_decoupage_en_morceaux(liste):
    for n in (1, 3, 17, 200):  # le découpage tombe n'importe où, y compris au milieu d'un mot ou d'un accent
        html = page_html(liste, morceaux=n)
        assert extraire_liste(html) == liste


def test_les_accents_et_guillemets_echappes_sont_restitues(liste):
    liste["items"][0]["productItem"]["title"] = 'Veste "Détroit" d\'été — 100 % coton\nligne 2'
    assert extraire_liste(page_html(liste))["items"][0]["productItem"]["title"] == liste["items"][0]["productItem"]["title"]


def test_morceau_illisible_ignore_sans_casser_les_autres(liste):
    html = page_html(liste, morceaux=1) + '<script>self.__next_f.push([1,"\\x invalide"])</script>'
    assert extraire_liste(html)["pagination"]["total_pages"] == 10
    assert "invalide" not in extraire_flux(html)


def test_page_sans_donnees_next_leve_une_erreur_claire():
    with pytest.raises(PageInattendue, match="introuvable"):
        analyser_page("<html><title>Vérification</title><body>Êtes-vous humain ?</body></html>", "vinted.fr")


def test_page_avec_json_tronque_leve_une_erreur_claire():
    html = '<script>self.__next_f.push([1,"{\\"items\\":{\\"items\\":[{\\"id\\":1,"])</script>'
    with pytest.raises(PageInattendue, match="illisible"):
        analyser_page(html, "vinted.fr")


# ------------------------------------------------------------------ annonces


def test_annonces_lues_et_illisibles_ignorees(page):
    assert [a.id for a in page.annonces] == [10777115810, 10777079895, 10777000003, 10777000004, 10777000005, 10777000006]
    assert page.brutes == 8 and page.illisibles == 2  # sans prix, sans identifiant


def test_pagination(page):
    assert (page.page, page.pages_total, page.total_resultats) == (1, 10, 960)


def test_annonce_reelle_carrahrt(page):
    a = par_id(page.annonces)[10777115810]
    assert a.titre == "Carrahrt Detroit jacket marron"  # titre mal orthographié, tel qu'en production
    assert a.prix == 45.0 and a.devise == "EUR"
    assert a.marque == "Carhartt" and a.taille == "L"
    assert a.etat == "tres_bon_etat" and a.etat_libelle == "Très bon état"
    assert a.url == "https://www.vinted.fr/items/10777115810-carrahrt-detroit-jacket-marron"
    assert a.photo_url == "https://images1.vinted.net/t/02_022c5_5jpf/f800/9a1.webp?s=bb"  # photo principale, grande taille
    assert a.nb_photos == 1 and a.vendeur.id == 1856372 and a.vendeur.pro is False
    assert a.publie_le is None  # absente de la liste
    assert a.description is None and a.detail_charge is False and a.vendeur.note is None


def test_vendeur_professionnel_et_plusieurs_photos(page):
    a = par_id(page.annonces)[10777079895]
    assert a.vendeur.pro is True and a.nb_photos == 3 and a.favoris == 3
    assert a.marque == "Carhartt WIP" and a.etat == "neuf_avec_etiquette"


def test_marque_absente_est_vide_et_non_pas_undefined(page):
    a = par_id(page.annonces)[10777000003]
    assert a.marque == "" and a.taille == "S" and a.etat == "bon_etat" and a.nb_photos == 2


def test_prix_remise_prioritaire_sur_le_prix_barre(page):
    assert par_id(page.annonces)[10777000005].prix == 20.0


def test_etat_seul_sans_taille(page):
    a = par_id(page.annonces)[10777000005]
    assert a.taille == "" and a.etat == "neuf_avec_etiquette" and a.nb_photos == 0 and a.photo_url is None


def test_etat_inconnu_conserve_son_libelle_et_la_taille_composee(page):
    a = par_id(page.annonces)[10777000006]
    assert a.etat is None and a.etat_libelle == "Etat mystérieux" and a.taille == "W32 L32"
    assert a.prix == 30.5 and a.nb_photos is None  # virgule décimale ; liste de photos absente = inconnu


def test_repli_sur_l_etiquette_d_accessibilite():
    a = analyser_entree({"productItem": {
        "id": 5, "title": "t", "price": {"amount": "10.00"},
        "itemBox": {"firstLine": "$undefined", "secondLine": "$undefined",
                    "accessibilityLabel": "t, Marque: Nike, Taille: M, État: Bon état, 10,00 €"}}}, "vinted.fr")
    assert (a.marque, a.taille, a.etat) == ("Nike", "M", "bon_etat")


def test_url_relative_absolue_ou_absente():
    base = {"id": 5, "title": "t", "price": {"amount": "1"}}
    assert analyser_entree({"productItem": {**base, "url": "/items/5-x"}}, "vinted.be").url == "https://www.vinted.be/items/5-x"
    assert analyser_entree({"productItem": {**base, "url": "https://www.vinted.fr/items/5-y"}}, "vinted.fr").url.endswith("5-y")
    assert analyser_entree({"productItem": base}, "vinted.es").url == "https://www.vinted.es/items/5"


def test_entree_sans_enveloppe_productitem_acceptee():
    a = analyser_entree({"id": 7, "title": "t", "price": {"amount": "3"}}, "vinted.fr")
    assert a.id == 7 and a.prix == 3.0


def test_entrees_inexploitables():
    with pytest.raises(ValueError, match="identifiant"):
        analyser_entree({"productItem": {"title": "x", "price": {"amount": "1"}}}, "vinted.fr")
    with pytest.raises(ValueError, match="prix"):
        analyser_entree({"productItem": {"id": 1, "title": "x"}}, "vinted.fr")


def test_liste_vide_est_valide():
    page = analyser_page(page_html({"items": [], "pagination": {"current_page": 1, "total_pages": 0}}), "vinted.fr")
    assert page.annonces == [] and page.brutes == 0


def test_la_liste_extraite_ne_contient_pas_de_donnees_de_session_du_reste_de_la_page(liste):
    """`capturer` n'enregistre que la liste : un jeton placé ailleurs dans la page n'y figure pas."""
    html = page_html(liste).replace("</body>", '<script>self.__next_f.push([1,"9:{\\"access_token\\":\\"JETON-SECRET\\"}"])</script></body>')
    assert "JETON-SECRET" in extraire_flux(html)
    assert "JETON-SECRET" not in json.dumps(analyser_page(html, "vinted.fr").liste)


def test_la_liste_conservee_ne_contient_que_annonces_et_pagination(liste):
    """L'objet de la page contient aussi des identifiants de session de suivi : `capturer` ne les enregistre pas."""
    assert "SECRET-SESSION" in json.dumps(extraire_liste(page_html(liste)))  # présent dans la page d'origine
    conservee = analyser_page(page_html(liste), "vinted.fr").liste
    assert set(conservee) == {"items", "pagination"} and "SECRET-SESSION" not in json.dumps(conservee)
