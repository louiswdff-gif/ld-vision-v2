from __future__ import annotations

import pytest

from bot_vinted.config import Backoff
from bot_vinted.vinted.pause import EtatPause


def pause(**kw) -> EtatPause:
    cfg = Backoff(initial_secondes=60, facteur=2, max_secondes=1800, alerte_apres=2, jitter_pct=0, **kw)
    return EtatPause(cfg, alea=lambda: 0.5)  # 0.5 → jitter nul


def test_delais_exponentiels_plafonnes():
    p = pause()
    assert [p.echec() for _ in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]


def test_retry_after_du_serveur_est_respecte_mais_plafonne():
    p = pause()
    assert p.echec(attente_serveur=300) == 300  # plus long que le backoff (60)
    assert p.echec(attente_serveur=10) == 120  # plus court : le backoff l'emporte
    assert p.echec(attente_serveur=99999) == 1800  # plafonné


def test_jitter_borne():
    cfg = Backoff(initial_secondes=100, facteur=2, max_secondes=1800, alerte_apres=2, jitter_pct=10)
    assert EtatPause(cfg, alea=lambda: 0.0).echec() == pytest.approx(90)  # -10 %
    assert EtatPause(cfg, alea=lambda: 1.0).echec() == pytest.approx(110)  # +10 %


def test_alerte_une_seule_fois_apres_n_echecs():
    p = pause()
    p.echec()
    assert p.a_alerter() is False  # seuil de 2 non atteint
    p.echec()
    assert p.a_alerter() is True
    p.marquer_alerte()
    p.echec()
    assert p.a_alerter() is False  # déjà alerté : pas de spam


def test_reprise_signalee_seulement_si_alerte_envoyee():
    p = pause()
    p.echec()
    assert p.succes() is False  # un seul échec, aucune alerte envoyée → pas de message « reprise »
    assert p.echecs == 0

    p.echec(); p.echec()
    p.marquer_alerte()
    assert p.succes() is True
    assert p.echecs == 0 and p.alerte_envoyee is False and p.en_difficulte is False
    assert p.echec() == 60  # le backoff repart de zéro
