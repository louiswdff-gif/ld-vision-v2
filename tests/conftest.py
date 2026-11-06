"""Fixtures pytest partagées. Aucun test n'appelle le vrai Vinted : tout passe par des fixtures JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOSSIER_FIXTURES = Path(__file__).parent / "fixtures"
RACINE_PROJET = Path(__file__).parent.parent


@pytest.fixture
def fixtures() -> Path:
    return DOSSIER_FIXTURES


@pytest.fixture
def charger_json():
    """charger_json("catalogue.json") → contenu décodé d'une fixture."""

    def _charger(nom: str):
        return json.loads((DOSSIER_FIXTURES / nom).read_text(encoding="utf-8"))

    return _charger


@pytest.fixture
def exemple_config_path() -> Path:
    return RACINE_PROJET / "config.exemple.yaml"


@pytest.fixture(autouse=True)
def _env_propre(monkeypatch):
    """Aucune variable de secret héritée de la machine de test."""
    for nom in ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_CARHARTT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(nom, raising=False)
