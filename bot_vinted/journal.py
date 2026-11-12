"""Journalisation : logs lisibles en console + fichier rotatif.

Sécurité : les webhooks Discord et les tokens Telegram sont des SECRETS. Un filtre les masque
dans tous les messages (console et fichier), même si une bibliothèque les écrit par erreur.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot_vinted.config import Config

# Motifs de secrets connus, masqués même si leur valeur n'a pas été enregistrée explicitement.
_MOTIFS_SECRETS = [
    (re.compile(r"(https://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/\d+/)[\w.-]+"), r"\1***"),
    (re.compile(r"(/bot\d+:)[\w-]+"), r"\1***"),
]
_MASQUE = "***"


class FiltreSecrets(logging.Filter):
    """Remplace les secrets par *** dans chaque message de log."""

    def __init__(self, valeurs: list[str] | None = None) -> None:
        super().__init__()
        # On masque d'abord les valeurs les plus longues (évite les masquages partiels).
        self._valeurs = sorted({v for v in (valeurs or []) if v and len(v) >= 8}, key=len, reverse=True)

    def masquer(self, texte: str) -> str:
        for valeur in self._valeurs:
            texte = texte.replace(valeur, _MASQUE)
        for motif, remplacement in _MOTIFS_SECRETS:
            texte = motif.sub(remplacement, texte)
        return texte

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # message mal formé : on ne perd pas la ligne
            message = str(record.msg)
        record.msg = self.masquer(message)
        record.args = ()
        if record.exc_info:  # masque aussi les secrets présents dans une trace d'erreur
            record.exc_text = self.masquer(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configurer_journal(cfg: Config, *, verbeux: bool = False) -> None:
    """Configure le logger racine : console lisible + fichier rotatif (taille et nombre configurables)."""
    niveau = logging.DEBUG if verbeux else getattr(logging, cfg.journal.niveau)
    racine = logging.getLogger()
    racine.setLevel(niveau)
    for h in list(racine.handlers):  # évite les doublons si appelé plusieurs fois (tests, relance)
        racine.removeHandler(h)

    filtre = FiltreSecrets(cfg.valeurs_secretes())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    console.addFilter(filtre)
    racine.addHandler(console)

    fichier = cfg.chemin(cfg.journal.fichier)
    try:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        rotatif = RotatingFileHandler(
            fichier,
            maxBytes=int(cfg.journal.taille_max_mo * 1024 * 1024),
            backupCount=cfg.journal.sauvegardes,
            encoding="utf-8",
        )
        rotatif.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        rotatif.addFilter(filtre)
        racine.addHandler(rotatif)
    except OSError as e:
        racine.warning("Impossible d'écrire le fichier de log %s (%s) : console uniquement.", fichier, e)

    # httpx journalise l'URL de chaque requête (celle de Telegram contient le token) : on le fait taire.
    for nom in ("httpx", "httpcore", "discord"):  # discord.py est bavard en INFO (connexion, reconnexions)
        logging.getLogger(nom).setLevel(logging.WARNING)


def chemin_journal(cfg: Config) -> Path:
    return cfg.chemin(cfg.journal.fichier)
