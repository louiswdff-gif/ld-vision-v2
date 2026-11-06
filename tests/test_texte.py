from __future__ import annotations

import pytest

from bot_vinted.texte import contient_mot, normaliser, premier_mot_present


def test_normaliser():
    assert normaliser("Veste CARHARTT  « Abîmée » !") == "veste carhartt abimee"
    assert normaliser("Levi's 501") == "levi s 501"
    assert normaliser(None) == "" and normaliser("") == ""


@pytest.mark.parametrize(
    "texte, mot, attendu",
    [
        ("veste type carhartt", "type", True),
        ("j ai trouve cette veste", "trou", False),  # mot entier : « trou » ≠ « trouve »
        ("trous aux manches", "trou", True),  # pluriel toléré
        ("grosse tache sur la manche", "tache", True),
        ("taches sur le col", "tache", True),
        ("veste abimee", "abîmé", True),  # féminin toléré, accents ignorés
        ("prototype", "type", False),
        ("vide dressing cause demenagement", "vide dressing", True),
        ("style vintage", "style", True),
        ("stylish", "style", False),  # mot entier uniquement
        ("", "x", False),
    ],
)
def test_contient_mot(texte, mot, attendu):
    assert contient_mot(normaliser(texte), mot) is attendu


def test_pluriel_peut_etre_desactive():
    assert contient_mot("taches", "tache", tolerer_pluriel=False) is False


def test_premier_mot_present():
    texte = normaliser("Veste neuve, un peu déchirée")
    assert premier_mot_present(texte, ["trou", "dechire"]) == "dechire"
    assert premier_mot_present(texte, ["trou"]) is None
