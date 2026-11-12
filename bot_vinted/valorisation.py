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
    source_revente: str  # « mediane » | « mediane_toutes_tailles » | « cible »
    nb_references: int  # nombre d'annonces de référence trouvées (même si insuffisant)
    mediane: float | None  # médiane brute unitaire (avant décote) si assez de références
    marge: float
    roi_pct: float
    ecart_pct: float  # écart du prix unitaire à la référence, en % (positif = moins cher que le marché)
    quantite: int = 1


def categorie_par_defaut(categories: list[int], repli: str = "0") -> str:
    """Catégorie de repli quand l'annonce n'indique pas la sienne : celle(s) de la recherche, sinon `repli`
    (le moteur passe le nom de la veille : deux veilles « pulls » et « polos » n'ont ainsi pas le même historique)."""
    return "-".join(str(c) for c in sorted(categories)) or repli


def categorie_de(annonce: Annonce, categories_recherche: list[int], repli: str = "0") -> str:
    """Catégorie servant de clé : celle de l'annonce si Vinted l'indique, sinon celle(s) de la recherche.

    À calculer UNE fois, sur l'annonce du catalogue (avant le chargement du détail), puis à réutiliser :
    ainsi l'historique et la valorisation d'une même annonce utilisent toujours la même clé."""
    return str(annonce.categorie_id) if annonce.categorie_id else categorie_par_defaut(categories_recherche, repli)


def cle_historique(annonce: Annonce, marque: str, categorie: str) -> CleHistorique:
    return CleHistorique(
        marque=marque or "inconnue",
        categorie=categorie or "0",
        taille=normaliser(annonce.taille) or "?",
        etat=annonce.etat or "inconnu",
    )


def cout_reel(prix: float, frais: FraisAcheteur, port: float) -> float:
    return round(prix + frais.fixe + prix * frais.pourcentage / 100 + port, 2)


def mediane_references(base: Base, annonce: Annonce, cle: CleHistorique, revente: Revente,
                       maintenant: float) -> tuple[float | None, int, str]:
    """(médiane brute, nombre de références, source). La médiane est None s'il y a moins de `min_references` annonces.

    Source « mediane » : même marque, taille et état. Si elles sont trop peu nombreuses et que `assouplir_taille`
    est actif, repli sur « mediane_toutes_tailles » (même marque, même veille, même état, toutes tailles)."""
    depuis = maintenant - revente.fenetre_jours * JOUR
    prix = base.prix_references(annonce.domaine, cle.marque, cle.categorie, cle.taille, cle.etat,
                                depuis=depuis, exclure_id=annonce.id)
    if len(prix) >= revente.min_references:
        return statistics.median(prix), len(prix), "mediane"
    if revente.assouplir_taille:
        large = base.prix_references_toutes_tailles(annonce.domaine, cle.marque, cle.categorie, cle.etat,
                                                    depuis=depuis, exclure_id=annonce.id)
        if len(large) >= revente.min_references:
            return statistics.median(large), len(large), "mediane_toutes_tailles"
    return None, len(prix), "cible"


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
    mediane, nb, source_mediane = mediane_references(base, annonce, cle, revente, maintenant)
    if mediane is not None:
        unitaire, source, reference = mediane * revente.decote, source_mediane, mediane
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
