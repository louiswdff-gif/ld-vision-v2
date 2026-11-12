"""La commande `run` de bout en bout (config réelle sur disque, faux Vinted, dry-run)."""

from __future__ import annotations

import logging
import shutil

import httpx
import pytest

from bot_vinted.base import Base
from bot_vinted.cli import principal
from bot_vinted.vinted.client import ClientVinted

from tests.conftest import RACINE_PROJET
from tests.outils import page_html

CONFIG = """
domaine: vinted.fr
journal: {fichier: logs/test.log, niveau: INFO}
base: {chemin: donnees/test.db}
reseau: {delai_min_entre_requetes_secondes: 0}
veilles:
  - nom: Carhartt vestes
    mots_cles: [carhartt, veste]
    prix_revente_cible: 70
"""


@pytest.fixture
def projet(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    shutil.copy(RACINE_PROJET / "marques.yaml", tmp_path / "marques.yaml")
    return tmp_path


@pytest.fixture(autouse=True)
def _journal_restaure():
    """`run` reconfigure le logger racine : on le remet en l'état pour ne pas perturber les autres tests."""
    racine = logging.getLogger()
    handlers, niveau = list(racine.handlers), racine.level
    yield
    for h in list(racine.handlers):
        racine.removeHandler(h)
        h.close()
    for h in handlers:
        racine.addHandler(h)
    racine.setLevel(niveau)


def faux_vinted(charger_json, requetes: list):
    def handler(requete: httpx.Request) -> httpx.Response:
        requetes.append(requete)
        if requete.url.path == "/":
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "_s=1; Domain=.vinted.fr; Path=/"})
        return httpx.Response(200, text=page_html(charger_json("catalogue_liste.json")),
                              headers={"content-type": "text/html"})

    def fabrique(reseau):
        async def pas_d_attente(_s: float) -> None:
            return None

        return ClientVinted(reseau, transport=httpx.MockTransport(handler), dormir=pas_d_attente)

    return fabrique


def test_run_dry_run_une_fois(projet, charger_json, monkeypatch, capsys):
    requetes: list = []
    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", faux_vinted(charger_json, requetes))

    code = principal(["--dry-run", "--une-fois", "--config", str(projet / "config.yaml")])

    assert code == 0
    # premier passage : pages d'amorçage lues tant qu'elles sont pleines ; ici la page contient 8 éléments < 96
    assert [r.url.path for r in requetes] == ["/", "/catalog"]
    assert requetes[1].url.params["search_text"] == "carhartt veste"
    # base et journal créés dans le dossier de la config ; le premier passage est enregistré
    with Base(projet / "donnees" / "test.db") as base:
        assert base.etat_veille("Carhartt vestes") is not None
        assert base.est_vue("vinted.fr", 10777115810)  # existant : marqué vu, sans alerte
        assert len(base.prix_references("vinted.fr", "carhartt", "carhartt vestes", "l", "tres_bon_etat", 0)) == 1
    assert (projet / "logs" / "test.log").is_file()
    assert "premier passage" in capsys.readouterr().out


def test_run_sans_secrets_refuse_hors_dry_run(projet, capsys):
    code = principal(["run", "--config", str(projet / "config.yaml")])
    err = capsys.readouterr().err
    assert code == 1 and "DISCORD_WEBHOOK_URL" in err and "--dry-run" in err


def test_run_sans_veille_refuse(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("domaine: vinted.fr\n", encoding="utf-8")
    assert principal(["run", "--dry-run", "--config", str(tmp_path / "config.yaml")]) == 1
    assert "Aucune veille" in capsys.readouterr().err


def test_run_marques_introuvable(projet, capsys):
    (projet / "marques.yaml").unlink()
    assert principal(["run", "--dry-run", "--une-fois", "--config", str(projet / "config.yaml")]) == 1
    assert "marques" in capsys.readouterr().err.lower()


# ------------------------------------------------------------------ avec le bot Discord interactif

CONFIG_BOT = CONFIG.replace("veilles:", "discord_bot: {actif: true, salon_id: 1, utilisateurs_autorises: [42]}\nveilles:")


@pytest.fixture
def bot_factice(monkeypatch):
    """Remplace la connexion à Discord : on enregistre seulement les appels de démarrage / d'arrêt."""
    pytest.importorskip("discord")
    from bot_vinted.discord_bot.bot import BotDiscord

    appels: list[str] = []

    async def demarrer(self):
        appels.append("demarrer")

    async def arreter(self):
        appels.append("arreter")

    monkeypatch.setattr(BotDiscord, "demarrer", demarrer)
    monkeypatch.setattr(BotDiscord, "arreter", arreter)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "jeton-bidon-123456")
    return appels


def test_run_demarre_puis_arrete_le_bot_discord(projet, charger_json, monkeypatch, bot_factice):
    (projet / "config.yaml").write_text(CONFIG_BOT, encoding="utf-8")
    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", faux_vinted(charger_json, []))
    assert principal(["--une-fois", "--config", str(projet / "config.yaml")]) == 0
    assert bot_factice == ["demarrer", "arreter"]


def test_run_dry_run_ne_demarre_pas_le_bot_discord(projet, charger_json, monkeypatch, bot_factice, capsys):
    (projet / "config.yaml").write_text(CONFIG_BOT, encoding="utf-8")
    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", faux_vinted(charger_json, []))
    monkeypatch.delenv("DISCORD_BOT_TOKEN")  # aucun secret nécessaire en simulation
    assert principal(["--dry-run", "--une-fois", "--config", str(projet / "config.yaml")]) == 0
    assert bot_factice == []
    assert "le bot Discord n'est pas démarré" in capsys.readouterr().out


def test_run_sans_token_du_bot_refuse(projet, monkeypatch, capsys):
    (projet / "config.yaml").write_text(CONFIG_BOT, encoding="utf-8")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert principal(["run", "--config", str(projet / "config.yaml")]) == 1
    err = capsys.readouterr().err
    assert "DISCORD_BOT_TOKEN" in err and "DISCORD_WEBHOOK" not in err  # le bot remplace les webhooks


def test_run_connexion_discord_impossible_est_une_erreur_claire(projet, charger_json, monkeypatch, bot_factice, capsys):
    from bot_vinted.config import ErreurConfig
    from bot_vinted.discord_bot.bot import BotDiscord

    async def refuse(self):
        raise ErreurConfig("Discord a refusé le token du bot : vérifiez DISCORD_BOT_TOKEN dans .env.")

    monkeypatch.setattr(BotDiscord, "demarrer", refuse)
    (projet / "config.yaml").write_text(CONFIG_BOT, encoding="utf-8")
    requetes: list = []
    monkeypatch.setattr("bot_vinted.vinted.client.ClientVinted", faux_vinted(charger_json, requetes))
    assert principal(["--une-fois", "--config", str(projet / "config.yaml")]) == 1
    assert "refusé le token" in capsys.readouterr().err
    assert requetes == []  # aucune requête vers Vinted tant que Discord n'est pas prêt
