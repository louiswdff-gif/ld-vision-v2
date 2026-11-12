from __future__ import annotations

import pytest

from bot_vinted.config import ErreurConfig, Veille, charger_config
from bot_vinted.vinted.url_recherche import UrlRechercheInvalide, analyser_url, recherche_de_la_veille


def test_url_de_l_exemple_utilisateur():
    p = analyser_url("https://www.vinted.fr/catalog?search_text=carhartt%20veste&order=newest_first")
    assert p.domaine == "vinted.fr"
    assert p.texte == "carhartt veste"
    assert p.marques == [] and p.categories == [] and p.prix_max is None
    assert p.avertissements == []


def test_url_complete_avec_crochets_non_encodes():
    url = (
        "https://www.vinted.fr/catalog?search_text=veste&brand_ids[]=362&brand_ids[]=53"
        "&catalog[]=2050&size_ids[]=207&size_ids[]=208&color_ids[]=12&material_ids[]=44"
        "&status_ids[]=6&status_ids[]=2&price_from=5&price_to=40.5&currency=EUR&order=newest_first"
    )
    p = analyser_url(url)
    assert p.marques == [362, 53]
    assert p.categories == [2050]
    assert p.tailles == [207, 208]
    assert p.couleurs == [12] and p.materiaux == [44]
    assert p.etats == ["neuf_avec_etiquette", "tres_bon_etat"]
    assert p.prix_min == 5 and p.prix_max == 40.5
    assert p.avertissements == []


def test_url_avec_crochets_encodes_et_ids_separes_par_des_virgules():
    p = analyser_url("https://www.vinted.fr/catalog?brand_ids%5B%5D=362&size_ids=207,208,207&status_ids%5B%5D=1")
    assert p.marques == [362]
    assert p.tailles == [207, 208]  # dédoublonné
    assert p.etats == ["neuf_sans_etiquette"]


def test_categorie_dans_le_chemin():
    p = analyser_url("https://www.vinted.fr/catalog/2050-jackets?search_text=carhartt")
    assert p.categories == [2050]
    # catégorie du chemin + paramètre catalog[] : union sans doublon
    p = analyser_url("https://www.vinted.fr/catalog/2050-jackets?catalog[]=2050&catalog[]=1776")
    assert p.categories == [2050, 1776]


@pytest.mark.parametrize("domaine", ["vinted.be", "vinted.es", "vinted.it", "vinted.co.uk", "vinted.com"])
def test_autres_domaines(domaine):
    assert analyser_url(f"https://www.{domaine}/catalog?search_text=nike").domaine == domaine


def test_url_sans_schema_est_acceptee():
    assert analyser_url("vinted.fr/catalog?search_text=nike").texte == "nike"


def test_hote_non_vinted_refuse():
    with pytest.raises(UrlRechercheInvalide, match="domaine"):
        analyser_url("https://www.example.com/catalog?search_text=nike")
    with pytest.raises(UrlRechercheInvalide):
        analyser_url("   ")


def test_avertissements_tri_et_parametres_inconnus_et_etat_inconnu():
    p = analyser_url("https://www.vinted.fr/catalog?search_text=a&order=price_low_to_high&foo=1&status_ids[]=99")
    textes = " | ".join(p.avertissements)
    assert "newest_first" in textes
    assert "foo" in textes
    assert "status_ids=99" in textes


def test_valeurs_invalides_ignorees_avec_avertissement():
    p = analyser_url("https://www.vinted.fr/catalog?brand_ids[]=abc&price_to=beaucoup")
    assert p.marques == [] and p.prix_max is None
    assert len(p.avertissements) == 2


def test_chemin_inattendu_signale():
    p = analyser_url("https://www.vinted.fr/items/123-veste")
    assert any("inattendu" in a for a in p.avertissements)


def test_parametres_de_la_page_ne_contiennent_pas_prix_ni_etats():
    veille = Veille(
        nom="t",
        url_recherche="https://www.vinted.fr/catalog?search_text=carhartt&brand_ids[]=362&brand_ids[]=53&catalog[]=2050"
                      "&price_to=40&status_ids[]=6",
    )
    r = recherche_de_la_veille(veille, "vinted.fr")
    assert r.parametres_page(page=2) == [
        ("order", "newest_first"), ("page", "2"), ("search_text", "carhartt"),
        ("brand_ids[]", "362"), ("brand_ids[]", "53"), ("catalog[]", "2050"),
    ]
    # …mais prix et état restent connus du bot pour le filtrage côté client
    assert r.prix_max == 40 and r.etats == ["neuf_avec_etiquette"]


def test_parametres_de_la_page_sans_texte_ni_filtre():
    r = recherche_de_la_veille(Veille(nom="t", marques=[1]), "vinted.fr")
    assert r.parametres_page() == [("order", "newest_first"), ("page", "1"), ("brand_ids[]", "1")]


def test_variante_retire_le_filtre_de_marque():
    r = recherche_de_la_veille(Veille(nom="t", mots_cles=["carhartt", "veste"], marques=[362], categories=[2050]), "vinted.fr")
    v = r.variante("carhart veste")
    assert v.texte == "carhart veste" and v.marques == [] and v.categories == [2050]
    assert ("brand_ids[]", "362") not in v.parametres_page() and ("catalog[]", "2050") in v.parametres_page()
    assert r.marques == [362]  # la recherche d'origine n'est pas modifiée


def test_champs_yaml_explicites_l_emportent_sur_l_url():
    veille = Veille(
        nom="t",
        url_recherche="https://www.vinted.be/catalog?search_text=nike&brand_ids[]=1&price_to=40&status_ids[]=6",
        mots_cles=["adidas", "veste"],
        marques=[99],
        prix_max=25,
        etats=["bon_etat"],
    )
    r = recherche_de_la_veille(veille, "vinted.fr")
    assert r.texte == "adidas veste"
    assert r.marques == [99]
    assert r.prix_max == 25
    assert r.etats == ["bon_etat"]
    assert r.domaine == "vinted.be"  # déduit de l'URL


def test_domaine_priorite_veille_puis_url_puis_global():
    sans_url = Veille(nom="a", mots_cles=["x"])
    assert recherche_de_la_veille(sans_url, "vinted.es").domaine == "vinted.es"
    avec_url = Veille(nom="b", url_recherche="https://www.vinted.it/catalog?search_text=x")
    assert recherche_de_la_veille(avec_url, "vinted.es").domaine == "vinted.it"
    force = Veille(nom="c", url_recherche="https://www.vinted.it/catalog?search_text=x", domaine="vinted.fr")
    assert recherche_de_la_veille(force, "vinted.es").domaine == "vinted.fr"


def test_url_invalide_dans_la_config_est_refusee_au_chargement(tmp_path):
    chemin = tmp_path / "config.yaml"
    chemin.write_text('veilles:\n  - nom: A\n    url_recherche: "https://exemple.com/x"\n', encoding="utf-8")
    with pytest.raises(ErreurConfig, match="veille « A »"):
        charger_config(chemin)


def test_config_exemple_recherche_effective(exemple_config_path):
    cfg = charger_config(exemple_config_path)
    carhartt, ralph = cfg.veilles
    assert cfg.recherche(carhartt).texte == "carhartt veste"
    assert cfg.recherche(carhartt).prix_max == 40
    assert cfg.recherche(ralph).texte == "ralph lauren pull"
    assert cfg.recherche(ralph).domaine == "vinted.fr"
