"""Couche achat-revente : coût réel, prix de revente estimé, marge nette, ROI.

1. Coût réel      = prix + (frais fixes + % du prix) de protection acheteur + port estimé (défini par veille).
2. Prix de revente = médiane des prix demandés vus pour (marque, catégorie, taille, état) × décote
                     (ce sont des prix DEMANDÉS, pas des prix vendus), si assez de références ;
                     sinon `prix_revente_cible` de la veille.
3. Marge nette    = revente − coût réel ;  ROI = marge / coût réel.
4. Lot de N articles : valeur unitaire × N × décote supplémentaire des lots.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from bot_vinted.base import Base
from bot_vinted.config import FraisAcheteur, Revente, Veille
from bot_vinted.texte import normaliser
from bot_vinted.vinted.modeles import Annonce

JOUR = 86400


@dataclass(frozen=True)
class CleHistorique:
    """Combinaison servant à regrouper les annonces comparables (toutes valeurs normalisées)."""

    marque: str
    categorie: str
    taille: str
    etat: str

    def avec_marque(self, marque: str) -> "CleHistorique":
        return CleHistorique(marque, self.categorie, self.taille, self.etat)


@dataclass(frozen=True)
class Valorisation:
    prix: float  # prix affiché de l'annonce (pour le lot entier)
    cout_reel: float
    revente_estimee: float  # pour l'ensemble de l'annonce (lot : N × valeur unitaire)
    source_revente: str  # « mediane » | « cible »
    nb_references: int  # nombre d'annonces de référence trouvées (même si insuffisant)
    mediane: float | None  # médiane brute unitaire (avant décote) si assez de références
    marge: float
    roi_pct: float
    ecart_pct: float  # écart du prix unitaire à la référence, en % (positif = moins cher que le marché)
    quantite: int = 1


def categorie_par_defaut(categories: list[int]) -> str:
    """Catégorie de repli quand l'annonce n'indique pas la sienne : celle(s) de la recherche."""
    return "-".join(str(c) for c in sorted(categories)) or "0"


def cle_historique(annonce: Annonce, marque: str, categorie_defaut: str) -> CleHistorique:
    return CleHistorique(
        marque=marque or "inconnue",
        categorie=str(annonce.categorie_id) if annonce.categorie_id else categorie_defaut,
        taille=normaliser(annonce.taille) or "?",
        etat=annonce.etat or "inconnu",
    )


def cout_reel(prix: float, frais: FraisAcheteur, port: float) -> float:
    return round(prix + frais.fixe + prix * frais.pourcentage / 100 + port, 2)


def mediane_references(base: Base, annonce: Annonce, cle: CleHistorique, revente: Revente,
                       maintenant: float) -> tuple[float | None, int]:
    """(médiane brute, nombre de références). La médiane est None s'il y a moins de `min_references` annonces."""
    prix = base.prix_references(
        annonce.domaine, cle.marque, cle.categorie, cle.taille, cle.etat,
        depuis=maintenant - revente.fenetre_jours * JOUR, exclure_id=annonce.id,
    )
    if len(prix) < revente.min_references:
        return None, len(prix)
    return statistics.median(prix), len(prix)


def estimer(
    annonce: Annonce,
    cle: CleHistorique,
    veille: Veille,
    frais: FraisAcheteur,
    revente: Revente,
    base: Base,
    maintenant: float,
    quantite: int = 1,
) -> Valorisation | None:
    """Valorise une annonce. Retourne None si aucun prix de revente n'est estimable
    (pas assez de références ET pas de `prix_revente_cible` dans la veille)."""
    mediane, nb = mediane_references(base, annonce, cle, revente, maintenant)
    if mediane is not None:
        unitaire, source, reference = mediane * revente.decote, "mediane", mediane
    elif veille.prix_revente_cible is not None:
        unitaire, source, reference = veille.prix_revente_cible, "cible", veille.prix_revente_cible
    else:
        return None

    quantite = max(1, quantite)
    revente_totale = unitaire * quantite * (revente.decote_lot if quantite > 1 else 1.0)
    cout = cout_reel(annonce.prix, frais, veille.port_estime or 0.0)
    marge = revente_totale - cout
    prix_unitaire = annonce.prix / quantite
    return Valorisation(
        prix=annonce.prix,
        cout_reel=cout,
        revente_estimee=round(revente_totale, 2),
        source_revente=source,
        nb_references=nb,
        mediane=round(mediane, 2) if mediane is not None else None,
        marge=round(marge, 2),
        roi_pct=round(marge / cout * 100, 1) if cout > 0 else 0.0,
        ecart_pct=round((reference - prix_unitaire) / reference * 100, 1),
        quantite=quantite,
    )
