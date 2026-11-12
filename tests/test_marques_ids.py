from __future__ import annotations

import json

import httpx
import pytest

from bot_vinted.cli import principal
from bot_vinted.vinted.client import ClientVinted
from bot_vinted.vinted.marques_ids import extraire_ids, resoudre, slugifier
from bot_vinted.vinted.page_catalogue import extraire_flux


def test_slugifier():
    assert slugifier("Levi's") == "levis" and slugifier("Ralph Lauren") == "ralph-lauren"
    assert slugifier("Arc'teryx") == "arcteryx" and slugifier("The North Face") == "the-north-face"
    assert slugifier("Stüssy") == "stussy" and slugifier("  Dr  Martens ") == "dr-martens"


def test_extraire_ids_depuis_html_et_donnees_nextjs():
    html = ('<a href="/brand/362-carhartt">C</a> <a href="/brand/88-ralph-lauren">R</a>'
            '<script>self.__next_f.push([1,' + json.dumps('{"url":"\\u002Fbrand\\u002F7000-patagonia"}') + '])</script>')
    assert extraire_ids(html + extraire_flux(html)) == {"carhartt": 362, "ralph-lauren": 88, "patagonia": 7000}


def test_premier_identifiant_conserve_pour_un_meme_slug():
    assert extraire_ids('/brand/10-levis /brand/99-levis') == {"levis": 10}


def test_resoudre_trouvees_manquantes_et_ordre():
    index = {"carhartt": 362, "ralph-lauren": 88, "levis": 10}
    trouvees, manquantes = resoudre(["Ralph Lauren", "Patagonia", "Levi's", "Carhartt", "Barbour"], index)
    assert list(trouvees.items()) == [("Ralph Lauren", 88), ("Levi's", 10), ("Carhartt", 362)]
    assert manquantes == ["Patagonia", "Barbour"]


def test_resoudre_sans_doublon_d_identifiant():
    trouvees, _ = resoudre(["levi's", "levis"], {"levis": 10})
    assert trouvees == {"levi's": 10}


# ------------------------------------------------------------------ commande


PAGE = ('<html><a href="/brand/362-carhartt">x</a><a href="/brand/88-ralph-lauren">x</a>'
        '<a href="/brand/10-levis">x</a></html>')


def faux_client(monkeypatch, handler):
    async def pas_d_attente(_s):
        return None

    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", lambda reseau: ClientVinted(
        reseau, transport=httpx.MockTransport(handler), dormir=pas_d_attente))


@pytest.fixture
def config_marques(tmp_path):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")
    (tmp_path / "marques.yaml").write_text("carhartt: [carhart]\nralph lauren: []\nlevi's: [levis]\npatagonia: []\n",
                                           encoding="utf-8")
    return str(tmp_path / "config.yaml")


def reponse(requete: httpx.Request) -> httpx.Response:
    if requete.url.path == "/":
        return httpx.Response(200, text="<html></html>", headers={"set-cookie": "_s=1; Domain=.vinted.fr; Path=/"})
    assert requete.url.path == "/brands"
    return httpx.Response(200, text=PAGE)


def test_cli_marques_ids(config_marques, monkeypatch, capsys):
    faux_client(monkeypatch, reponse)
    assert principal(["marques-ids", "--config", config_marques]) == 0
    sortie = capsys.readouterr().out
    assert "3 marques connues de Vinted ; 3 trouvée(s) sur 4 demandée(s)" in sortie
    assert "362  carhartt" in sortie and "88  ralph lauren" in sortie and "10  levi's" in sortie
    assert "patagonia" in sortie.split("Introuvables")[1]  # signalée comme à cocher à la main
    assert "marques: [362, 88, 10]" in sortie


def test_cli_marques_ids_liste_restreinte(config_marques, monkeypatch, capsys):
    faux_client(monkeypatch, reponse)
    assert principal(["marques-ids", "--marques", "Carhartt, Levi's", "--config", config_marques]) == 0
    assert "marques: [362, 10]" in capsys.readouterr().out


def test_cli_marques_ids_bloque_ou_format_change(config_marques, monkeypatch, capsys):
    faux_client(monkeypatch, lambda r: httpx.Response(200, text="<html></html>") if r.url.path == "/" else httpx.Response(403))
    assert principal(["marques-ids", "--config", config_marques]) == 2
    assert "Réessayez plus tard" in capsys.readouterr().err
    faux_client(monkeypatch, lambda r: httpx.Response(200, text="<html>rien</html>"))
    assert principal(["marques-ids", "--config", config_marques]) == 2
    assert "aucun identifiant lisible" in capsys.readouterr().err


def test_cli_marques_ids_exclut_par_defaut_les_marques_a_ne_pas_surveiller(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")
    (tmp_path / "marques.yaml").write_text(
        "carhartt: [carhart]\nstone island:\n  variantes: [stoneisland]\n  scanner: false\n", encoding="utf-8")
    faux_client(monkeypatch, lambda r: httpx.Response(200, text="<html></html>") if r.url.path == "/" else
                httpx.Response(200, text='<a href="/brand/362-carhartt">c</a><a href="/brand/1-stone-island">s</a>'))
    config = str(tmp_path / "config.yaml")
    assert principal(["marques-ids", "--config", config]) == 0
    defaut = capsys.readouterr().out
    assert "marques: [362]" in defaut and "stone island" not in defaut
    assert principal(["marques-ids", "--toutes", "--config", config]) == 0
    assert "marques: [362, 1]" in capsys.readouterr().out
