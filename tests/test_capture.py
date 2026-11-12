"""Commande `capturer` : une requête, JSON brut enregistré, rapport du parseur. Tout sur un faux Vinted."""

from __future__ import annotations

import json

import httpx
import pytest

from bot_vinted.capture import ErreurCapture, _cible, capturer
from bot_vinted.cli import principal
from bot_vinted.config import Config
from bot_vinted.vinted.client import ClientVinted, VintedBloque
from tests.outils import page_html


@pytest.fixture
def requetes() -> list[httpx.Request]:
    return []


@pytest.fixture
def faux_vinted(charger_json, requetes):
    def handler(requete: httpx.Request) -> httpx.Response:
        requetes.append(requete)
        chemin = requete.url.path
        if chemin == "/":
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "_s=1; Domain=.vinted.fr; Path=/"})
        if chemin == "/catalog":
            return httpx.Response(200, text=page_html(charger_json("catalogue_liste.json")),
                                  headers={"content-type": "text/html"})
        if chemin.startswith("/api/v2/items/"):
            return httpx.Response(200, json=charger_json("detail_annonce.json"))
        return httpx.Response(200, json={"hello": "world"})

    return handler


def client(faux_vinted) -> ClientVinted:
    async def pas_d_attente(_s: float) -> None:
        return None

    return ClientVinted(Config().reseau, transport=httpx.MockTransport(faux_vinted), dormir=pas_d_attente)


# ------------------------------------------------------------------ choix de la requête


def test_cible_recherche_vinted():
    domaine, chemin, params, type_ = _cible(
        Config(), "https://www.vinted.fr/catalog?search_text=carhartt&brand_ids[]=362&price_to=40&status_ids[]=6")
    assert (domaine, chemin, type_) == ("vinted.fr", "/catalog", "catalogue")
    assert ("search_text", "carhartt") in params and ("brand_ids[]", "362") in params
    assert not any(cle in ("price_to", "status_ids[]") for cle, _ in params)  # mêmes paramètres que ceux du bot


def test_cible_annonce_et_api_directe():
    assert _cible(Config(), "https://www.vinted.be/items/4001-veste-carhartt")[:2] == ("vinted.be", "/api/v2/items/4001")
    assert _cible(Config(), "https://www.vinted.fr/api/v2/users/77")[:2] == ("vinted.fr", "/api/v2/users/77")
    assert _cible(Config(domaine="vinted.es"), "/api/v2/users/77?localize=true") == \
           ("vinted.es", "/api/v2/users/77", {"localize": "true"}, "brut")


def test_cible_invalide():
    with pytest.raises(ErreurCapture):
        _cible(Config(), "https://www.example.com/catalog?search_text=x")
    with pytest.raises(ErreurCapture, match="identifiant"):
        _cible(Config(), "https://www.vinted.fr/items/pas-un-id")


# ------------------------------------------------------------------ capture


async def test_capture_d_une_recherche(faux_vinted, requetes, tmp_path):
    sortie = tmp_path / "sous" / "catalogue_reel.json"
    async with client(faux_vinted) as c:
        rapport = await capturer(Config(), c, "https://www.vinted.fr/catalog?search_text=carhartt", sortie)

    enregistre = json.loads(sortie.read_text(encoding="utf-8"))
    assert set(enregistre) == {"items", "pagination"}  # annonces + pagination, rien d'autre (pas d'identifiants de session)
    assert "SECRET-SESSION" not in sortie.read_text(encoding="utf-8")
    assert enregistre["items"][0]["productItem"]["title"] == "Carrahrt Detroit jacket marron"  # accents conservés
    assert "6 annonce(s) lue(s) sur 8, 2 illisible(s)" in rapport and "page 1/10, 960 résultats" in rapport
    assert "Carrahrt Detroit jacket marron" in rapport and "tres_bon_etat" in rapport and "marque « Carhartt »" in rapport
    assert "date de publication" in rapport  # rappel de ce que la liste ne contient pas
    assert "tests/fixtures" in rapport
    assert [r.url.path for r in requetes] == ["/", "/catalog"]  # UNE seule requête de données


async def test_capture_de_page_inexploitable_ne_cree_pas_de_fichier(requetes, tmp_path):
    def handler(requete):
        requetes.append(requete)
        return httpx.Response(200, text="<html>Vérification</html>")

    async with client(handler) as c:
        rapport = await capturer(Config(), c, "https://www.vinted.fr/catalog?search_text=x", tmp_path / "x.json")
    assert "inexploitable" in rapport and not (tmp_path / "x.json").exists()


async def test_capture_d_une_annonce(faux_vinted, tmp_path):
    async with client(faux_vinted) as c:
        rapport = await capturer(Config(), c, "https://www.vinted.fr/items/4001-veste", tmp_path / "detail.json")
    assert "5 photo(s)" in rapport and "marie_d" in rapport and "142 avis" in rapport and "FR" in rapport


async def test_capture_api_directe(faux_vinted, tmp_path):
    async with client(faux_vinted) as c:
        rapport = await capturer(Config(), c, "https://www.vinted.fr/api/v2/something", tmp_path / "x.json")
    assert "hello" in rapport


async def test_capture_api_directe_au_format_inattendu(tmp_path):
    def handler(requete):
        if requete.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json={"results": [], "autre": 1})

    async with client(handler) as c:
        rapport = await capturer(Config(), c, "https://www.vinted.fr/api/v2/quelque_chose", tmp_path / "x.json")
    assert "Clés de premier niveau : autre, results" in rapport


async def test_capture_bloquee_ne_reessaie_pas(tmp_path, requetes):
    def handler(requete):
        requetes.append(requete)
        if requete.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(429)

    async with client(handler) as c:
        with pytest.raises(VintedBloque):
            await capturer(Config(), c, "https://www.vinted.fr/catalog?search_text=x", tmp_path / "x.json")
    assert [r.url.path for r in requetes].count("/catalog") == 1
    assert not (tmp_path / "x.json").exists()


# ------------------------------------------------------------------ CLI


def test_cli_capturer(monkeypatch, faux_vinted, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")
    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", lambda reseau: client(faux_vinted))
    sortie = tmp_path / "capture.json"
    code = principal(["capturer", "https://www.vinted.fr/catalog?search_text=carhartt", str(sortie),
                      "--config", str(tmp_path / "config.yaml")])
    assert code == 0 and sortie.is_file() and "annonce(s) lue(s)" in capsys.readouterr().out


def test_cli_capturer_bloque_code_2(monkeypatch, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")

    def handler(requete):
        return httpx.Response(200, text="<html></html>") if requete.url.path == "/" else httpx.Response(403)

    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", lambda reseau: client(handler))
    code = principal(["capturer", "https://www.vinted.fr/items/1-x", str(tmp_path / "c.json"),
                      "--config", str(tmp_path / "config.yaml")])
    assert code == 2 and "ne contourne aucune protection" in capsys.readouterr().err


def test_cli_capturer_url_invalide_code_1(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")
    code = principal(["capturer", "https://www.example.com/x", str(tmp_path / "c.json"),
                      "--config", str(tmp_path / "config.yaml")])
    assert code == 1
