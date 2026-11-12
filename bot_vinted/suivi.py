"""Suivi des résultats réels : commandes `acheter`, `vendu` et `stats`.

Sémantique (volontairement simple, sans estimation) :
- `acheter <id> <prix>` : <prix> = TOTAL réellement payé, tout compris (article + protection acheteur + port) ;
- `vendu <id> <prix>`   : <prix> = montant NET réellement reçu ;
- marge réelle = vendu − acheté ; ROI = marge / acheté.

Les statistiques servent à régler les seuils (`marge_min`, `score_min`, `revente.decote`) en comparant
la marge ESTIMÉE par le bot à la marge RÉELLE obtenue.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bot_vinted.base import Base
from bot_vinted.vinted.constantes import normaliser_domaine

JOUR = 86400
SANS_VEILLE = "(sans veille)"


class ErreurSuivi(Exception):
    """Erreur d'usage des commandes de suivi (message lisible)."""


# --------------------------------------------------------------------------- identification de l'annonce


def extraire_reference(valeur: str) -> tuple[int, str | None]:
    """« 4001 » ou « https://www.vinted.fr/items/4001-veste » → (4001, None | « vinted.fr »)."""
    texte = valeur.strip()
    if texte.isdigit():
        return int(texte), None
    m = re.search(r"/items/(\d+)", texte)
    if not m:
        raise ErreurSuivi(f"« {valeur} » n'est ni un identifiant d'annonce ni une URL d'annonce Vinted (/items/<id>-…).")
    domaine = None
    if "://" in texte:
        try:
            domaine = normaliser_domaine(urlsplit(texte).netloc)
        except ValueError:
            domaine = None
    return int(m.group(1)), domaine


def _choisir_domaine(explicite: str | None, de_l_url: str | None, candidats: list[str], defaut: str,
                     annonce_id: int) -> str:
    if explicite:
        return normaliser_domaine(explicite)
    if de_l_url:
        return de_l_url
    domaines = sorted(set(candidats))
    if len(domaines) == 1:
        return domaines[0]
    if len(domaines) > 1:
        raise ErreurSuivi(f"L'annonce {annonce_id} existe sur plusieurs domaines ({', '.join(domaines)}) : "
                          "précisez-le avec --domaine.")
    return defaut


# --------------------------------------------------------------------------- acheter / vendu


def acheter(base: Base, reference: str, prix_total: float, maintenant: float, *, domaine_defaut: str,
            titre: str | None = None, veille: str | None = None, domaine: str | None = None) -> str:
    """Enregistre (ou corrige) un achat. Retourne le message à afficher."""
    annonce_id, domaine_url = extraire_reference(reference)
    vues = base.annonces_par_id(annonce_id)
    domaine = _choisir_domaine(domaine, domaine_url, [r["domaine"] for r in vues], domaine_defaut, annonce_id)
    vue = base.annonce(domaine, annonce_id)

    existant = base.achat(domaine, annonce_id)
    base.enregistrer_achat(
        domaine, annonce_id,
        veille=vue["veille"] if vue else veille,
        titre=vue["titre"] if vue else titre,
        prix_total=prix_total, maintenant=maintenant,
        marge_estimee=vue["marge"] if vue else None,
    )
    verbe = "corrigé" if existant else "enregistré"
    lignes = [f"Achat {verbe} : annonce {annonce_id} ({domaine}) pour {prix_total:.2f} € tout compris."]
    if vue:
        lignes.append(f"  « {vue['titre']} » — veille « {vue['veille']} », marge estimée par le bot : "
                      f"{vue['marge']:+.2f} €.")
    else:
        lignes.append("  (annonce jamais vue par le bot : pas de marge estimée ni de veille associée)")
    return "\n".join(lignes)


def vendre(base: Base, reference: str, prix_net: float, maintenant: float, *, domaine_defaut: str,
           domaine: str | None = None) -> str:
    """Enregistre la revente d'un article acheté. Retourne le message à afficher."""
    annonce_id, domaine_url = extraire_reference(reference)
    achats = base.achats_par_id(annonce_id)
    if not achats:
        raise ErreurSuivi(f"Aucun achat enregistré pour l'annonce {annonce_id} : utilisez d'abord "
                          f"`python main.py acheter {annonce_id} <prix payé>`.")
    domaine = _choisir_domaine(domaine, domaine_url, [r["domaine"] for r in achats], domaine_defaut, annonce_id)
    achat = base.achat(domaine, annonce_id)
    if achat is None:
        raise ErreurSuivi(f"Aucun achat enregistré pour l'annonce {annonce_id} sur {domaine}.")
    try:
        base.enregistrer_vente(domaine, annonce_id, prix_net, maintenant)
    except ValueError as e:
        raise ErreurSuivi(str(e)) from e

    paye = achat["prix_achat_total"]
    marge = prix_net - paye
    roi = marge / paye * 100 if paye > 0 else 0.0
    jours = (maintenant - achat["date_achat"]) / JOUR
    lignes = [
        f"Vente enregistrée : annonce {annonce_id} ({domaine}) revendue {prix_net:.2f} € net "
        f"(acheté {paye:.2f} €).",
        f"  Marge réelle : {marge:+.2f} € · ROI : {roi:+.0f} % · délai de revente : {jours:.1f} jour(s).",
    ]
    if achat["marge_estimee"] is not None:
        lignes.append(f"  Marge estimée par le bot : {achat['marge_estimee']:+.2f} € "
                      f"(écart réel − estimé : {marge - achat['marge_estimee']:+.2f} €).")
    return "\n".join(lignes)


# --------------------------------------------------------------------------- statistiques


@dataclass
class StatsVeille:
    veille: str
    achats: int = 0
    vendus: int = 0
    marge_totale: float = 0.0
    rois: list[float] = field(default_factory=list)
    delais: list[float] = field(default_factory=list)

    @property
    def roi_moyen(self) -> float | None:
        return statistics.fmean(self.rois) if self.rois else None

    @property
    def delai_moyen(self) -> float | None:
        return statistics.fmean(self.delais) if self.delais else None


@dataclass
class Stats:
    achats: int = 0
    vendus: int = 0
    en_stock: int = 0
    capital_immobilise: float = 0.0
    total_achete_vendus: float = 0.0  # somme payée pour les articles revendus
    marge_totale: float = 0.0
    marge_moyenne: float | None = None
    roi_moyen: float | None = None  # moyenne des ROI par article
    roi_global: float | None = None  # marge totale / total acheté (articles revendus)
    delai_moyen_jours: float | None = None
    marge_estimee_moyenne: float | None = None  # sur les articles vendus ayant une estimation du bot
    marge_reelle_moyenne_comparable: float | None = None  # marge réelle moyenne des mêmes articles
    nb_comparables: int = 0
    par_veille: list[StatsVeille] = field(default_factory=list)  # triées : plus rentable d'abord


def calculer_stats(achats: list[sqlite3.Row]) -> Stats:
    s = Stats(achats=len(achats))
    veilles: dict[str, StatsVeille] = {}
    marges: list[float] = []
    rois: list[float] = []
    delais: list[float] = []
    estimees: list[float] = []
    reelles_comparables: list[float] = []

    for a in achats:
        sv = veilles.setdefault(a["veille"] or SANS_VEILLE, StatsVeille(a["veille"] or SANS_VEILLE))
        sv.achats += 1
        if a["date_vente"] is None:
            s.en_stock += 1
            s.capital_immobilise += a["prix_achat_total"]
            continue
        paye, vendu = a["prix_achat_total"], a["prix_vente_net"]
        marge = vendu - paye
        roi = marge / paye * 100 if paye > 0 else 0.0
        delai = (a["date_vente"] - a["date_achat"]) / JOUR
        s.vendus += 1
        s.total_achete_vendus += paye
        marges.append(marge)
        rois.append(roi)
        delais.append(delai)
        sv.vendus += 1
        sv.marge_totale += marge
        sv.rois.append(roi)
        sv.delais.append(delai)
        if a["marge_estimee"] is not None:
            estimees.append(a["marge_estimee"])
            reelles_comparables.append(marge)

    if marges:
        s.marge_totale = sum(marges)
        s.marge_moyenne = statistics.fmean(marges)
        s.roi_moyen = statistics.fmean(rois)
        s.roi_global = s.marge_totale / s.total_achete_vendus * 100 if s.total_achete_vendus > 0 else None
        s.delai_moyen_jours = statistics.fmean(delais)
    if estimees:
        s.nb_comparables = len(estimees)
        s.marge_estimee_moyenne = statistics.fmean(estimees)
        s.marge_reelle_moyenne_comparable = statistics.fmean(reelles_comparables)
    # Les veilles qui ont des ventes d'abord (par marge décroissante) ; celles sans aucune vente en fin de liste :
    # une marge de 0 € « faute de données » ne doit pas passer devant une veille qui a réellement perdu de l'argent.
    s.par_veille = sorted(veilles.values(), key=lambda v: (v.vendus > 0, v.marge_totale, v.vendus), reverse=True)
    return s


def formater_stats(s: Stats) -> str:
    if s.achats == 0:
        return ("Aucun achat enregistré pour l'instant.\n"
                "Utilisez `python main.py acheter <id> <prix payé tout compris>` après un achat, "
                "puis `python main.py vendu <id> <prix net reçu>` à la revente.")

    def eur(x: float | None, signe: bool = False) -> str:
        return "—" if x is None else f"{x:+.2f} €" if signe else f"{x:.2f} €"

    def pct(x: float | None) -> str:
        return "—" if x is None else f"{x:+.0f} %"

    def jours(x: float | None) -> str:
        return "—" if x is None else f"{x:.1f} j"

    lignes = [
        "═══ Résultats réels ═══",
        f"Achats : {s.achats} ({s.vendus} revendu(s), {s.en_stock} en stock · capital immobilisé {eur(s.capital_immobilise)})",
        f"Marge réelle totale : {eur(s.marge_totale, signe=True)} · marge moyenne par article : {eur(s.marge_moyenne, signe=True)}",
        f"ROI moyen : {pct(s.roi_moyen)} (ROI global {pct(s.roi_global)}) · délai moyen de revente : {jours(s.delai_moyen_jours)}",
    ]
    if s.nb_comparables:
        ecart = s.marge_reelle_moyenne_comparable - s.marge_estimee_moyenne
        lignes += [
            "",
            f"═══ Estimation du bot vs réalité ({s.nb_comparables} article(s) comparables) ═══",
            f"Marge estimée moyenne : {eur(s.marge_estimee_moyenne, signe=True)} · "
            f"marge réelle moyenne : {eur(s.marge_reelle_moyenne_comparable, signe=True)} · écart : {eur(ecart, signe=True)}",
            conseil_calibrage(s.marge_estimee_moyenne, ecart),
        ]
    lignes += ["", "═══ Veilles les plus rentables ═══"]
    for i, v in enumerate(s.par_veille, 1):
        lignes.append(
            f"{i}. {v.veille} : {eur(v.marge_totale, signe=True)} sur {v.vendus} vente(s) "
            f"(ROI moyen {pct(v.roi_moyen)}, délai {jours(v.delai_moyen)}, {v.achats - v.vendus} en stock)"
        )
    return "\n".join(lignes)


def conseil_calibrage(estimee: float, ecart: float) -> str:
    """Interprétation de l'écart estimé/réel, pour régler les seuils."""
    if estimee <= 0:
        return "→ Marges estimées nulles ou négatives : pas de conclusion possible."
    ratio = ecart / estimee
    if ratio < -0.25:
        return ("→ Le bot SURESTIME vos marges : baissez `revente.decote` (ex. 0.80) ou relevez `marge_min` / "
                "`score_min` pour ne garder que les meilleures affaires.")
    if ratio > 0.25:
        return ("→ Le bot SOUS-ESTIME vos marges : vous pouvez remonter `revente.decote` ou assouplir `marge_min` "
                "pour voir plus d'affaires.")
    return "→ Estimations cohérentes avec la réalité : vos réglages actuels semblent bien calibrés."
