from __future__ import annotations

import pytest

from bot_vinted.config import Marques, Signaux
from bot_vinted.marques import CatalogueMarques
from bot_vinted.signaux import Lot, annonce_mal_valorisee, detecter_lot, vente_pressee
from bot_vinted.vinted.modeles import Annonce

from tests.conftest import RACINE_PROJET

MOTS = Signaux().mots_vente_pressee


@pytest.fixture(scope="module")
def marques() -> CatalogueMarques:
    return CatalogueMarques.charger(RACINE_PROJET / "marques.yaml", Marques())


def annonce(titre: str, marque: str = "", nb_photos: int | None = None) -> Annonce:
    return Annonce(domaine="vinted.fr", id=1, titre=titre, url="u", prix=10.0, marque=marque, nb_photos=nb_photos)


# ------------------------------------------------------------------ lots


@pytest.mark.parametrize("titre, quantite", [
    ("Lot de 3 pulls Ralph Lauren", 3),
    ("Lot de 5 t-shirts Nike", 5),
    ("lot 4 polos", 4),
    ("Pack de 2 sweats", 2),
    ("Nike chaussettes x3", 3),
    ("Sweat Adidas 2x", 2),
    ("5 pulls hiver taille M", 5),
    ("LOT DE 12 JEANS", 12),
])
def test_lot_avec_quantite(titre, quantite):
    assert detecter_lot(titre, 30) == Lot(quantite)


@pytest.mark.parametrize("titre", ["Lot de vêtements homme", "Bundle vêtements", "Lot", "Pack hiver"])
def test_lot_sans_quantite(titre):
    assert detecter_lot(titre, 30) == Lot(None)


@pytest.mark.parametrize("titre", [
    "Veste Carhartt Detroit",
    "Nike Air Max 90",
    "Pull 1 pièce",  # un seul article n'est pas un lot
    "Polo Lacoste taille 5",
    "Pantalon x1",
])
def test_pas_un_lot(titre):
    assert detecter_lot(titre, 30) is None


def test_quantite_incoherente_ignoree():
    assert detecter_lot("Lot de 99 pulls", 30) == Lot(None)  # au-delà de quantite_max_lot : lot mais quantité inconnue
    assert detecter_lot("Lot de 99 pulls", 100) == Lot(99)


# ------------------------------------------------------------------ vente pressée


@pytest.mark.parametrize("texte, mot", [
    ("URGENT veste à vendre", "urgent"),
    ("Vente suite à déménagement", "demenagement"),
    ("Vide dressing : tout doit partir", "vide dressing"),
    ("Pull à saisir", "a saisir"),
    ("Je déménage, vente urgente", "urgent"),  # le féminin est toléré : « urgente » déclenche « urgent »
])
def test_vente_pressee(texte, mot):
    assert vente_pressee(texte, MOTS) == mot


def test_pas_de_vente_pressee():
    assert vente_pressee("Veste Carhartt très bon état", MOTS) is None
    assert vente_pressee("", MOTS) is None
    assert vente_pressee("Urgence médicale", MOTS) is None  # mot entier : « urgence » ≠ « urgent »


def test_mots_vente_pressee_configurables():
    assert vente_pressee("Cadeau de départ", ["cadeau"]) == "cadeau"


# ------------------------------------------------------------------ annonce mal mise en valeur


def test_titre_vague_sans_marque_ni_modele(marques):
    assert annonce_mal_valorisee(annonce("Veste"), Signaux(), marques) == ["titre vague"]
    assert annonce_mal_valorisee(annonce("Veste homme"), Signaux(), marques) == ["titre vague"]
    assert annonce_mal_valorisee(annonce("Beau pull chaud"), Signaux(), marques) == ["titre vague"]


def test_titre_precis_n_est_pas_vague(marques):
    assert annonce_mal_valorisee(annonce("Veste Carhartt"), Signaux(), marques) == []  # marque dans le titre
    assert annonce_mal_valorisee(annonce("Jean 501"), Signaux(), marques) == []  # modèle (chiffres)
    # marque seulement dans le champ, pas dans le titre : le titre reste vague
    assert annonce_mal_valorisee(annonce("Veste", marque="Zara"), Signaux(), marques) == ["titre vague"]
    # marque inconnue de marques.yaml ET absente du champ : le titre court reste jugé vague
    assert annonce_mal_valorisee(annonce("Veste Zara"), Signaux(), marques) == ["titre vague"]


def test_marque_du_champ_citee_dans_le_titre(marques):
    assert annonce_mal_valorisee(annonce("Veste Zara", marque="Zara"), Signaux(), marques) == []


def test_titre_long_non_generique_n_est_pas_vague(marques):
    assert annonce_mal_valorisee(annonce("Magnifique manteau long en laine bleu marine"), Signaux(), marques) == []


def test_titre_long_mais_uniquement_generique(marques):
    s = Signaux(titre_vague_mots_max=1)
    assert annonce_mal_valorisee(annonce("Veste manteau pull"), s, marques) == ["titre vague"]


def test_une_ou_deux_photos(marques):
    s = Signaux()
    assert annonce_mal_valorisee(annonce("Veste Carhartt Detroit", nb_photos=1), s, marques) == ["1 photo"]
    assert annonce_mal_valorisee(annonce("Veste Carhartt Detroit", nb_photos=2), s, marques) == ["2 photos"]
    assert annonce_mal_valorisee(annonce("Veste Carhartt Detroit", nb_photos=3), s, marques) == []
    assert annonce_mal_valorisee(annonce("Veste Carhartt Detroit", nb_photos=None), s, marques) == []  # inconnu


def test_titre_vague_et_peu_de_photos(marques):
    assert annonce_mal_valorisee(annonce("Veste", nb_photos=1), Signaux(), marques) == ["titre vague", "1 photo"]


def test_seuil_photos_configurable(marques):
    assert annonce_mal_valorisee(annonce("Veste Carhartt Detroit", nb_photos=3), Signaux(photos_max=3), marques) == ["3 photos"]
