"""Évaluation complète d'une annonce : marque effective → valorisation → signaux pépite → deal score.

Fonction pure par rapport au réseau : le moteur l'appelle une première fois avec les seules données du
catalogue (pour décider s'il vaut la peine de charger le détail), puis une seconde fois avec la description,
le nombre de photos et le vendeur.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot_vinted.base import Base
from bot_vinted.config import Config, Veille
from bot_vinted.marques import AnalyseMarque, CatalogueMarques
from bot_vinted.score import ResultatScore, calculer_score
from bot_vinted.signaux import Lot, annonce_mal_valorisee, detecter_lot, vente_pressee
from bot_vinted.valorisation import CleHistorique, Valorisation, cle_historique, estimer
from bot_vinted.vinted.modeles import Annonce


@dataclass(frozen=True)
class Evaluation:
    valorisation: Valorisation
    score: ResultatScore
    marque: AnalyseMarque
    cle: CleHistorique
    lot: Lot | None
    age_minutes: float | None
    signaux: list[str] = field(default_factory=list)  # codes (marque_mal_orthographiee, vente_pressee…)
    tags: list[str] = field(default_factory=list)  # libellés affichés dans l'alerte


def evaluer(
    annonce: Annonce,
    veille: Veille,
    categorie: str,
    cfg: Config,
    base: Base,
    marques: CatalogueMarques,
    maintenant: float,
) -> Evaluation | None:
    """Évalue une annonce. None si son prix de revente n'est pas estimable (ni références ni prix cible)."""
    texte = f"{annonce.titre} {annonce.description or ''}"
    marque = marques.analyser(annonce.marque, texte)
    cle = cle_historique(annonce, marque.marque, categorie)

    lot = detecter_lot(annonce.titre, cfg.revente.quantite_max_lot)
    quantite = lot.quantite if lot and lot.quantite else 1

    valo = estimer(annonce, cle, veille, cfg.frais_acheteur, cfg.revente, base, maintenant, quantite)
    if valo is None:
        return None

    bonus_cfg = cfg.score.bonus
    signaux: list[str] = []
    tags: list[str] = []
    bonus = 0.0

    if marque.signal == "mal_orthographiee":
        signaux.append("marque_mal_orthographiee")
        tags.append(f"🔤 Marque mal orthographiée (« {marque.trouve} » → {marque.nom_affiche})")
        bonus += bonus_cfg.marque_mal_orthographiee
    elif marque.signal == "absente_du_champ":
        signaux.append("marque_absente_du_champ")
        tags.append(f"🏷️ Marque absente du champ ({marque.nom_affiche})")
        bonus += bonus_cfg.marque_absente_du_champ

    raisons = annonce_mal_valorisee(annonce, cfg.signaux, marques)
    if raisons:
        signaux.append("annonce_mal_valorisee")
        tags.append(f"🔍 Annonce mal mise en valeur ({', '.join(raisons)})")
        bonus += bonus_cfg.annonce_mal_valorisee

    mot = vente_pressee(texte, cfg.signaux.mots_vente_pressee)
    if mot:
        signaux.append("vente_pressee")
        tags.append(f"⏱️ Vente pressée (« {mot} »)")
        bonus += bonus_cfg.vente_pressee

    if lot:
        if lot.quantite:
            signaux.append("lot")
            tags.append(f"📦 Lot de {lot.quantite} (valeur estimée à l'unité)")
            bonus += bonus_cfg.lot
        else:
            tags.append("📦 Lot (quantité inconnue : valeur non détaillée)")

    age = annonce.age_minutes(maintenant)
    score = calculer_score(valo, annonce, age, bonus, cfg.score)
    return Evaluation(valorisation=valo, score=score, marque=marque, cle=cle, lot=lot, age_minutes=age,
                      signaux=signaux, tags=tags)
