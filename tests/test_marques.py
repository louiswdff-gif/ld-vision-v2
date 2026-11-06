from __future__ import annotations

import pytest

from bot_vinted.config import ErreurConfig, Marques
from bot_vinted.marques import CatalogueMarques

from tests.conftest import RACINE_PROJET


@pytest.fixture(scope="module")
def marques() -> CatalogueMarques:
    return CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", Marques())


def test_fichier_marques_du_projet_est_charge(marques):
    assert len(marques) >= 30
    assert "carharrt" in marques.variantes_de("carhartt")
    assert "ralf lauren" in marques.variantes_de("ralph lauren")
    assert marques.variantes_de("marque inconnue") == []


# ------------------------------------------------------------------ détection


def test_ecriture_exacte(marques):
    d = marques.detecter("Veste CARHARTT Detroit taille L")
    assert (d.marque, d.exacte, d.methode, d.score) == ("carhartt", True, "exacte", 100.0)
    d = marques.detecter("Pull Ralph Lauren col rond")
    assert d.marque == "ralph lauren" and d.exacte


@pytest.mark.parametrize("texte, marque, trouve", [
    ("Veste carharrt workwear", "carhartt", "carharrt"),  # exemples du cahier des charges
    ("Veste carhart marron", "carhartt", "carhart"),
    ("Pull ralf lauren bleu", "ralph lauren", "ralf lauren"),
    ("Polo ralph loren", "ralph lauren", "ralph loren"),
    ("Doudoune north face noire", "the north face", "north face"),
    ("Sweat northface", "the north face", "northface"),
    ("Docmartens 1460", "dr martens", "docmartens"),
])
def test_variantes_listees(marques, texte, marque, trouve):
    d = marques.detecter(texte)
    assert d.marque == marque and d.exacte is False and d.methode == "variante" and d.trouve == trouve


@pytest.mark.parametrize("texte, marque", [
    ("Veste carhartr", "carhartt"),  # faute non listée : fuzzy
    ("Veste carhaart", "carhartt"),
    ("Pull ralph laurem", "ralph lauren"),
    ("Doudoune patagoina", "patagonia"),
    ("Sweat stone islnd", "stone island"),
    ("Jean calvin kleinn", "calvin klein"),
])
def test_fautes_de_frappe_par_fuzzy(marques, texte, marque):
    d = marques.detecter(texte)
    assert d is not None and d.marque == marque and d.methode == "fuzzy" and d.exacte is False
    assert d.score >= 86


@pytest.mark.parametrize("texte", [
    "Veste en laine homme taille M",
    "Pull col roulé gris chiné",
    "Carte cadeau",  # proche de rien
    "Nikes vintage",  # marque trop courte pour le fuzzy (« nike » = 4 lettres)
    "Chaussures de randonnée",
    "",
])
def test_pas_de_faux_positifs(marques, texte):
    assert marques.detecter(texte) is None


def test_fuzzy_desactive_par_marque(marques):
    assert marques.detecter("moteur diesek") is None  # pas de fuzzy sur « diesel »
    assert marques.detecter("jean diezel").marque == "diesel"  # mais la variante listée reste reconnue


def test_seuil_fuzzy_configurable():
    strict = CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", Marques(seuil_fuzzy=95))
    assert strict.detecter("Veste carhartr") is None
    souple = CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", Marques(seuil_fuzzy=80))
    assert souple.detecter("Veste carhartr").marque == "carhartt"


def test_accents_et_ponctuation_ignores(marques):
    assert marques.detecter("STÜSSY hoodie").marque == "stussy"
    assert marques.detecter("Veste Arc'teryx").marque == "arc teryx"
    assert marques.detecter("Jean Levi's 501").marque == "levi s"
    assert marques.detecter("Jean levis 501").marque == "levi s"


def test_exacte_prioritaire_sur_variante(marques):
    d = marques.detecter("carhart ou carhartt ?")
    assert d.marque == "carhartt" and d.exacte is True


# ------------------------------------------------------------------ analyse champ + texte


def test_champ_marque_reconnu_pas_de_signal(marques):
    a = marques.analyser("Carhartt", "Veste Carhartt Detroit")
    assert (a.marque, a.signal, a.source) == ("carhartt", None, "champ")


def test_champ_vide_et_marque_dans_le_titre_est_une_pepite(marques):
    a = marques.analyser("", "Veste Carhartt workwear")
    assert (a.marque, a.signal, a.source, a.nom_affiche) == ("carhartt", "absente_du_champ", "texte", "carhartt")


def test_champ_autre_equivaut_a_vide(marques):
    a = marques.analyser("Autre", "Veste Carhartt workwear")
    assert a.signal == "absente_du_champ" and a.marque == "carhartt"


def test_marque_mal_orthographiee_dans_le_titre(marques):
    a = marques.analyser("", "Carhart veste workwear")
    assert (a.marque, a.signal, a.trouve) == ("carhartt", "mal_orthographiee", "carhart")


def test_champ_marque_lui_meme_mal_ecrit(marques):
    a = marques.analyser("Carharrt", "Veste")
    assert (a.marque, a.signal, a.source) == ("carhartt", "mal_orthographiee", "champ")


def test_champ_renseigne_est_autoritaire(marques):
    # « type Carhartt » dans le titre d'un Nike ne doit pas faire croire à une pépite Carhartt
    a = marques.analyser("Nike", "Sweat Nike type Carhartt")
    assert (a.marque, a.signal) == ("nike", None)
    # marque non répertoriée dans marques.yaml : on fait confiance au champ
    a = marques.analyser("Zara", "Veste Zara style Carhartt")
    assert (a.marque, a.signal, a.source) == ("zara", None, "champ")


def test_aucune_marque_trouvee(marques):
    a = marques.analyser("", "Veste")
    assert (a.marque, a.signal, a.source) == ("inconnue", None, "aucune")


# ------------------------------------------------------------------ chargement


def test_fichier_introuvable(tmp_path):
    with pytest.raises(ErreurConfig, match="introuvable"):
        CatalogueMarques.charger(tmp_path / "absent.yaml", Marques())


def test_forme_longue_et_cles_inconnues(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("mamarque:\n  variantes: [mamark]\n  fuzzy: false\n", encoding="utf-8")
    c = CatalogueMarques.charger(f, Marques())
    assert c.detecter("une mamark").marque == "mamarque"
    f.write_text("mamarque:\n  variantez: [x]\n", encoding="utf-8")
    with pytest.raises(ErreurConfig, match="clés inconnues"):
        CatalogueMarques.charger(f, Marques())
