"""Backoff exponentiel GLOBAL : toutes les veilles partagent la même IP, donc la même pause.

Logique pure (aucun réseau, aucune horloge) pour être testée facilement. Le moteur l'utilise ainsi :
  - échec 429/403/réseau  → `delai = pause.echec(retry_after)` puis il attend `delai` secondes ;
  - dès `pause.a_alerter()`  → un SEUL message « bot en pause » (puis `pause.marquer_alerte()`) ;
  - premier succès après alerte → `pause.succes()` renvoie True : message « reprise ».
"""

from __future__ import annotations

import random
from collections.abc import Callable

from bot_vinted.config import Backoff


class EtatPause:
    def __init__(self, cfg: Backoff, alea: Callable[[], float] = random.random) -> None:
        self._cfg = cfg
        self._alea = alea
        self.echecs = 0
        self.alerte_envoyee = False

    def echec(self, attente_serveur: float | None = None) -> float:
        """Enregistre un échec et retourne le délai (secondes) à respecter avant de réessayer.

        délai = initial × facteur^(échecs précédents), plafonné à `max_secondes`, au moins égal à
        l'attente demandée par le serveur (en-tête Retry-After), avec un petit jitter ±jitter_pct %.
        """
        c = self._cfg
        delai = min(c.initial_secondes * (c.facteur**self.echecs), c.max_secondes)
        if attente_serveur and attente_serveur > 0:
            delai = max(delai, min(attente_serveur, c.max_secondes))
        delai *= 1 + (self._alea() * 2 - 1) * c.jitter_pct / 100
        self.echecs += 1
        return max(1.0, delai)

    def a_alerter(self) -> bool:
        """Vrai une seule fois : quand le nombre d'échecs consécutifs atteint le seuil d'alerte."""
        return self.echecs >= self._cfg.alerte_apres and not self.alerte_envoyee

    def marquer_alerte(self) -> None:
        self.alerte_envoyee = True

    def succes(self) -> bool:
        """Réinitialise. Retourne True si une alerte « en pause » avait été envoyée (→ prévenir de la reprise)."""
        avait_alerte = self.alerte_envoyee
        self.echecs = 0
        self.alerte_envoyee = False
        return avait_alerte

    @property
    def en_difficulte(self) -> bool:
        return self.echecs > 0
