from __future__ import annotations

import pytest

from bot_vinted.config import Config, Veille
from bot_vinted.filtres import (
    filtrer_apres_detail,
    filtrer_avant_detail,
    mots_exclus_effectifs,
    verifier_etat,
    verifier_exclusions,
    verifier_photos,
    verifier_prix,
    verifier_vendeur,
)
from bot_vinted.vinted.modeles import Annonce, Vendeur
from bot_vinted.vinted.url_recherche import RechercheEffective


def annonce(**kw) -> Annonce:
    base = dict(domaine="vinted.fr", id=1, titre="Veste Carhartt Detroit", url="https://www.vinted.fr/items/1",
                prix=28.0, marque="Carhartt", taille="L", etat="tres_bon_etat")
    base.update(kw)
    return Annonce(**base)


def recherche(**kw) -> RechercheEffective:
    base = dict(domaine="vinted.fr", texte="carhartt", marques=[], categories=[], tailles=[], couleurs=[],
                materiaux=[], etats=[], prix_min=None, prix_max=None)
    base.update(kw)
    return RechercheEffective(**base)


def veille(**kw) -> Veille:
    return Veille(**{"nom": "T", "mots_cles": ["carhartt"], **kw})


@pytest.fixture
def cfg() -> Config:
    return Config()


# ------------------------------------------------------------------ prix et état


def test_prix_dans_la_fourchette():
    r = recherche(prix_min=10, prix_max=40)
    assert verifier_prix(annonce(prix=28), r) is None
    assert verifier_prix(annonce(prix=10), r) is None and verifier_prix(annonce(prix=40), r) is None  # bornes incluses
    assert verifier_prix(annonce(prix=9.99), r).code == "prix_min"
    assert verifier_prix(annonce(prix=40.01), r).code == "prix_max"
    assert verifier_prix(annonce(prix=999), recherche()) is None  # pas de borne


def test_etat():
    r = recherche(etats=["neuf_avec_etiquette", "tres_bon_etat"])
    assert verifier_etat(annonce(etat="tres_bon_etat"), r) is None
    assert verifier_etat(annonce(etat="bon_etat"), r).code == "etat"
    assert verifier_etat(annonce(etat=None, etat_libelle="???"), r).code == "etat_inconnu"
    assert verifier_etat(annonce(etat=None), recherche()) is None  # aucun état exigé : on accepte tout


# ------------------------------------------------------------------ mots exclus et liste globale


@pytest.mark.parametrize("titre, mot_attendu", [
    ("Veste type Carhartt", "type"),
    ("Pull style Ralph Lauren", "style"),
    ("Sac Gucci REPLICA", "replica"),
    ("Veste réplique Nike", "replique"),
    ("Jean copie Levi's", "copie"),
    ("Sneakers fake", "fake"),
    ("Dupe Stanley cup", "dupe"),
    ("Veste Inspired by Barbour", "inspired"),
    ("Veste avec un trou à la manche", "trou"),
    ("Pull avec taches", "tache"),
    ("Manteau abîmé", "abime"),
    ("Veste déchirée au col", "dechire"),
])
def test_liste_d_exclusion_globale_du_cahier_des_charges(cfg, titre, mot_attendu):
    rejet = verifier_exclusions(titre, "le titre", veille(), cfg)
    assert rejet is not None and rejet.code == "mot_exclu" and mot_attendu in rejet.detail


def test_pas_de_faux_positif_sur_des_mots_voisins(cfg):
    for titre in ("Veste Carhartt trouvée en vide-grenier", "Prototype Nike", "Pull stylish", "Chemise à rayures"):
        assert verifier_exclusions(titre, "le titre", veille(), cfg) is None, titre


def test_mots_exclus_de_la_veille(cfg):
    v = veille(mots_exclus=["enfant"])
    rejet = verifier_exclusions("Veste Carhartt enfant 10 ans", "le titre", v, cfg)
    assert rejet.code == "mot_exclu" and "mot exclu" in rejet.detail
    assert verifier_exclusions("Veste Carhartt adulte", "le titre", v, cfg) is None


def test_exception_autorisee_par_la_veille(cfg):
    sans_exception = veille()
    avec_exception = veille(exceptions_exclusion=["style", "type"])
    assert verifier_exclusions("Pull style vintage", "le titre", sans_exception, cfg) is not None
    assert verifier_exclusions("Pull style vintage", "le titre", avec_exception, cfg) is None
    assert verifier_exclusions("Veste type workwear", "le titre", avec_exception, cfg) is None
    # …mais les autres exclusions restent actives
    assert verifier_exclusions("Pull style vintage avec trou", "le titre", avec_exception, cfg).detail.endswith("le titre")
    assert verifier_exclusions("Sac replica", "le titre", avec_exception, cfg) is not None


def test_exception_insensible_aux_accents_et_a_la_casse(cfg):
    v = veille(exceptions_exclusion=["RÉPLIQUE"])
    assert verifier_exclusions("Réplique de l'épée", "le titre", v, cfg) is None
    assert ("replique", "contrefaçon") not in [(m, o) for m, o in mots_exclus_effectifs(v, cfg)]
    assert "replique" not in [m for m, _ in mots_exclus_effectifs(v, cfg)]


def test_liste_effective_contient_veille_contrefacons_et_defauts(cfg):
    mots = dict(mots_exclus_effectifs(veille(mots_exclus=["enfant"]), cfg))
    assert mots["enfant"] == "mot exclu" and mots["fake"] == "contrefaçon" and mots["trou"] == "défaut"


# ------------------------------------------------------------------ vendeur et photos


def vendeur(**kw) -> Vendeur:
    return Vendeur(**{"id": 1, "login": "x", "note": 4.8, "nb_avis": 20, "pays": "FR", **kw})


def test_vendeur_note_avis_pays():
    v = veille(vendeur_note_min=4.5, vendeur_avis_min=5, vendeur_pays=["fr", "be"])
    assert verifier_vendeur(vendeur(), v) is None
    assert verifier_vendeur(vendeur(note=4.4), v).code == "vendeur_note"
    assert verifier_vendeur(vendeur(nb_avis=4), v).code == "vendeur_avis"
    assert verifier_vendeur(vendeur(pays="IT"), v).code == "vendeur_pays"
    assert verifier_vendeur(vendeur(pays="BE"), v) is None  # codes pays normalisés en majuscules


def test_vendeur_donnee_exigee_mais_inconnue_est_rejetee():
    assert verifier_vendeur(None, veille(vendeur_note_min=4.0)).code == "vendeur_note_inconnue"
    assert verifier_vendeur(vendeur(note=None, nb_avis=0), veille(vendeur_note_min=4.0)).code == "vendeur_note_inconnue"
    assert verifier_vendeur(vendeur(nb_avis=None), veille(vendeur_avis_min=3)).code == "vendeur_avis_inconnus"
    assert verifier_vendeur(vendeur(pays=None), veille(vendeur_pays=["FR"])).code == "vendeur_pays_inconnu"


def test_sans_filtre_vendeur_tout_passe():
    assert verifier_vendeur(None, veille()) is None
    assert verifier_vendeur(vendeur(note=None, nb_avis=0), veille()) is None


def test_photos_minimum():
    v = veille(photos_min=3)
    assert verifier_photos(annonce(nb_photos=3), v) is None
    assert verifier_photos(annonce(nb_photos=2), v).code == "photos"
    assert verifier_photos(annonce(nb_photos=None), v) is None  # inconnu avant détail : on ne rejette pas
    assert verifier_photos(annonce(nb_photos=1), veille()) is None


# ------------------------------------------------------------------ orchestration


def test_avant_detail_enchaine_les_filtres(cfg):
    v = veille()
    r = recherche(prix_max=40, etats=["tres_bon_etat"])
    assert filtrer_avant_detail(annonce(), v, r, cfg) is None
    assert filtrer_avant_detail(annonce(prix=50), v, r, cfg).code == "prix_max"
    assert filtrer_avant_detail(annonce(etat="bon_etat"), v, r, cfg).code == "etat"
    assert filtrer_avant_detail(annonce(titre="Veste fake Carhartt"), v, r, cfg).code == "mot_exclu"
    assert filtrer_avant_detail(annonce(visible=False), v, r, cfg).code == "invisible"


def test_la_marque_est_aussi_verifiee_pour_les_exclusions(cfg):
    assert filtrer_avant_detail(annonce(marque="Replica"), veille(), recherche(), cfg).code == "mot_exclu"


def test_apres_detail_description_photos_vendeur(cfg):
    v = veille(vendeur_note_min=4.5, photos_min=3)
    ok = annonce(description="Très belle veste", nb_photos=4, vendeur=vendeur())
    assert filtrer_apres_detail(ok, v, cfg) is None
    assert filtrer_apres_detail(ok.model_copy(update={"description": "petit trou sur la manche"}), v, cfg).code == "mot_exclu"
    assert filtrer_apres_detail(ok.model_copy(update={"nb_photos": 1}), v, cfg).code == "photos"
    assert filtrer_apres_detail(ok.model_copy(update={"vendeur": vendeur(note=3.0)}), v, cfg).code == "vendeur_note"
