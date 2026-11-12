"""Telegram sans Discord : configuration, diffusion, `telegram-id` et `tester`."""

from __future__ import annotations

import httpx
import pytest

from bot_vinted.alertes import ErreurEnvoi, construire_diffuseur
from bot_vinted.alertes.telegram import CanalTelegram, decouvrir_conversations
from bot_vinted.cli import principal
from bot_vinted.config import Config, ErreurConfig, charger_config

TOKEN = "123456:TOKEN_TELEGRAM_SECRET"


def ecrire(tmp_path, contenu: str):
    chemin = tmp_path / "config.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


CONFIG_TELEGRAM = "discord: {actif: false}\ntelegram: {actif: true}\nveilles:\n  - {nom: A, mots_cles: [carhartt]}\n"


# ------------------------------------------------------------------ configuration


def test_telegram_seul_n_exige_que_ses_deux_variables(tmp_path):
    cfg = charger_config(ecrire(tmp_path, CONFIG_TELEGRAM))
    assert set(cfg.variables_secretes()) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}  # plus de webhook Discord exigé


def test_au_moins_un_canal_doit_etre_actif(tmp_path):
    with pytest.raises(ErreurConfig, match="aucun canal d'alerte actif"):
        charger_config(ecrire(tmp_path, "discord: {actif: false}\n"))


def test_par_defaut_discord_reste_actif():
    assert Config().discord.actif is True and Config().variables_secretes() == ["DISCORD_WEBHOOK_URL"]


def test_diffuseur_telegram_seul(tmp_path):
    cfg = charger_config(ecrire(tmp_path, CONFIG_TELEGRAM))
    assert [c.nom for c in construire_diffuseur(cfg, dry_run=False).canaux] == ["telegram"]
    assert [c.nom for c in construire_diffuseur(cfg, dry_run=True).canaux] == ["console"]
    both = Config(telegram={"actif": True})
    assert [c.nom for c in construire_diffuseur(both, dry_run=False).canaux] == ["discord", "telegram"]


# ------------------------------------------------------------------ découverte de l'identifiant


def reponse_updates(*conversations):
    return httpx.Response(200, json={"ok": True, "result": [{"update_id": i, "message": {"chat": c, "text": "salut"}}
                                                            for i, c in enumerate(conversations)]})


async def test_decouvrir_conversations():
    transport = httpx.MockTransport(lambda r: reponse_updates(
        {"id": 987654321, "type": "private", "first_name": "Louis", "last_name": "D"},
        {"id": 987654321, "type": "private", "first_name": "Louis"},  # doublon : une seule entrée
        {"id": -100555, "type": "group", "title": "Mes alertes"},
    ))
    assert await decouvrir_conversations(TOKEN, transport=transport) == [
        (987654321, "Louis (private)"), (-100555, "Mes alertes (group)")]


async def test_aucune_conversation_trouvee():
    assert await decouvrir_conversations(TOKEN, transport=httpx.MockTransport(lambda r: reponse_updates())) == []


@pytest.mark.parametrize("code, message", [(401, "refuse ce token"), (404, "refuse ce token"), (500, "HTTP 500")])
async def test_erreurs_telegram_sans_jamais_citer_le_token(code, message):
    with pytest.raises(ErreurEnvoi, match=message) as e:
        await decouvrir_conversations(TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(code)))
    assert "TOKEN_TELEGRAM_SECRET" not in str(e.value)


async def test_telegram_injoignable_sans_citer_le_token():
    def coupure(r):
        raise httpx.ConnectError("boom", request=r)

    with pytest.raises(ErreurEnvoi) as e:
        await decouvrir_conversations(TOKEN, transport=httpx.MockTransport(coupure))
    assert "TOKEN_TELEGRAM_SECRET" not in str(e.value)


def test_cli_telegram_id(tmp_path, monkeypatch, capsys):
    chemin = ecrire(tmp_path, CONFIG_TELEGRAM)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    async def trouve(token):
        assert token == TOKEN
        return [(987654321, "Louis (private)")]

    monkeypatch.setattr("bot_vinted.alertes.telegram.decouvrir_conversations", trouve)
    assert principal(["telegram-id", "--config", str(chemin)]) == 0
    sortie = capsys.readouterr().out
    assert "TELEGRAM_CHAT_ID=987654321" in sortie and "TOKEN_TELEGRAM_SECRET" not in sortie


def test_cli_telegram_id_sans_conversation_explique_quoi_faire(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    async def rien(token):
        return []

    monkeypatch.setattr("bot_vinted.alertes.telegram.decouvrir_conversations", rien)
    assert principal(["telegram-id", "--config", str(ecrire(tmp_path, CONFIG_TELEGRAM))]) == 1
    assert "envoyez-lui un message" in capsys.readouterr().err


def test_cli_telegram_id_sans_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert principal(["telegram-id", "--config", str(ecrire(tmp_path, CONFIG_TELEGRAM))]) == 1
    assert "BotFather" in capsys.readouterr().err


# ------------------------------------------------------------------ tester


def test_tester_envoie_un_message_et_une_alerte_et_le_dit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    envois: list[httpx.Request] = []

    def handler(requete):
        envois.append(requete)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("bot_vinted.alertes.telegram.CanalTelegram",
                        lambda cfg: CanalTelegram(cfg, transport=httpx.MockTransport(handler)))
    assert principal(["tester", "--config", str(ecrire(tmp_path, CONFIG_TELEGRAM))]) == 0
    assert [r.url.path.rsplit("/", 1)[1] for r in envois] == ["sendMessage", "sendMessage"]  # message système, alerte
    corps = " ".join(r.content.decode() for r in envois)
    assert "ESSAI" in corps and "fonctionne" in corps and "rien à acheter" in corps
    assert "✓ telegram" in capsys.readouterr().out


def test_tester_signale_l_echec_d_un_canal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    refus = httpx.MockTransport(lambda r: httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    monkeypatch.setattr("bot_vinted.alertes.telegram.CanalTelegram", lambda cfg: CanalTelegram(cfg, transport=refus))
    assert principal(["tester", "--config", str(ecrire(tmp_path, CONFIG_TELEGRAM))]) == 1
    err = capsys.readouterr().err
    assert "✗ telegram" in err and "chat not found" in err and "TOKEN_TELEGRAM_SECRET" not in err


def test_tester_refuse_si_un_secret_manque(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert principal(["tester", "--config", str(ecrire(tmp_path, CONFIG_TELEGRAM))]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
