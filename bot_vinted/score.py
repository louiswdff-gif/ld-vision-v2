"""Deal score (0-100).

Moyenne pondérée de six critères notés de 0 à 100, puis bonus « pépite » (plafonné), le tout borné à [0, 100].

  écart à la médiane · marge nette (€) · ROI · état · fraîcheur · fiabilité du vendeur

Tous les poids, plafonds et paliers viennent de config.yaml (section `score`).
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_vinted.config import Score
from bot_vinted.valorisation import Valorisation
from bot_vinted.vinted.modeles import Annonce, Vendeur

NEUTRE = 50.0  # note d'un critère dont la donnée est inconnue


@dataclass(frozen=True)
class ResultatScore:
    total: int  # 0-100
    criteres: dict[str, float]  # sous-score 0-100 de chaque critère
    bonus: float  # points de bonus « pépite » réellement ajoutés (après plafond)
    palier: str  # « 🔥 » | « ✅ » | « 🔹 »


def _borne(x: float, bas: float = 0.0, haut: float = 100.0) -> float:
    return max(bas, min(haut, x))


def score_vendeur(vendeur: Vendeur | None, avis_confiance: int) -> float:
    """Note du vendeur (0-5 → 0-100), tirée vers le neutre tant que le nombre d'avis est faible."""
    if vendeur is None or vendeur.note is None:
        return NEUTRE
    confiance = _borne((vendeur.nb_avis or 0) / avis_confiance, 0.0, 1.0)
    return confiance * (vendeur.note / 5 * 100) + (1 - confiance) * NEUTRE


def palier(score: float, cfg: Score) -> str:
    if score >= cfg.seuils.feu:
        return "🔥"
    if score >= cfg.seuils.ok:
        return "✅"
    return "🔹"


def calculer_score(
    valo: Valorisation,
    annonce: Annonce,
    age_minutes: float | None,
    bonus_pepites: float,
    cfg: Score,
) -> ResultatScore:
    criteres = {
        "ecart_mediane": _borne(valo.ecart_pct / cfg.ecart_plein_pct * 100),
        "marge": _borne(valo.marge / cfg.marge_pleine_eur * 100),
        "roi": _borne(valo.roi_pct / cfg.roi_plein_pct * 100),
        "etat": _borne(cfg.etats.get(annonce.etat or "", NEUTRE)),
        "fraicheur": NEUTRE if age_minutes is None else 100 * 0.5 ** (age_minutes / cfg.fraicheur_demi_vie_minutes),
        "vendeur": score_vendeur(annonce.vendeur, cfg.vendeur_avis_confiance),
    }
    poids = cfg.poids.model_dump()
    somme_poids = sum(poids.values())
    base = sum(criteres[k] * poids[k] for k in criteres) / somme_poids if somme_poids > 0 else 0.0
    bonus = min(max(bonus_pepites, 0.0), cfg.bonus.plafond)
    total = round(_borne(base + bonus))
    return ResultatScore(total=total, criteres={k: round(v, 1) for k, v in criteres.items()}, bonus=bonus,
                         palier=palier(total, cfg))
