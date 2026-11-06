"""Filtres d'une veille et liste d'exclusion globale.

Le filtrage se fait en DEUX temps pour économiser les requêtes :
1. `filtrer_avant_detail` : uniquement avec les données du catalogue (prix, état, titre, marque) ;
2. `filtrer_apres_detail` : avec ce que seul le détail de l'annonce fournit (description, nombre de photos,
   note / avis / pays du vendeur). Il n'est appelé que pour les annonces qui ont déjà passé le test de marge.

Chaque filtre retourne un `Rejet` (code + explication lisible, utile dans les logs DEBUG) ou None.
Les mots exclus sont comparés par MOT ENTIER, sans accents ni casse, pluriels/féminins tolérés.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_vinted.config import Config, Veille
from bot_vinted.texte import normaliser, premier_mot_present
from bot_vinted.vinted.modeles import Annonce, Vendeur
from bot_vinted.vinted.url_recherche import RechercheEffective


@dataclass(frozen=True)
class Rejet:
    code: str  # identifiant stable (ex. « prix_max »)
    detail: str  # explication lisible

    def __str__(self) -> str:
        return f"{self.code} : {self.detail}"


def mots_exclus_effectifs(veille: Veille, cfg: Config) -> list[tuple[str, str]]:
    """(mot, origine) à refuser pour cette veille : mots exclus de la veille + liste globale − exceptions."""
    exceptions = {normaliser(m) for m in veille.exceptions_exclusion}
    mots: list[tuple[str, str]] = [(m, "mot exclu") for m in veille.mots_exclus]
    mots += [(m, "contrefaçon") for m in cfg.exclusions_globales.contrefacons if normaliser(m) not in exceptions]
    mots += [(m, "défaut") for m in cfg.exclusions_globales.defauts if normaliser(m) not in exceptions]
    return mots


def verifier_exclusions(texte: str, endroit: str, veille: Veille, cfg: Config) -> Rejet | None:
    """Cherche un mot exclu dans `texte` (titre ou description). `endroit` : « titre » / « description »."""
    texte_norm = normaliser(texte)
    if not texte_norm:
        return None
    for mot, origine in mots_exclus_effectifs(veille, cfg):
        if premier_mot_present(texte_norm, [mot]):
            return Rejet("mot_exclu", f"{origine} « {mot} » dans {endroit}")
    return None


def verifier_prix(annonce: Annonce, recherche: RechercheEffective) -> Rejet | None:
    if recherche.prix_min is not None and annonce.prix < recherche.prix_min:
        return Rejet("prix_min", f"{annonce.prix:g} € < minimum {recherche.prix_min:g} €")
    if recherche.prix_max is not None and annonce.prix > recherche.prix_max:
        return Rejet("prix_max", f"{annonce.prix:g} € > maximum {recherche.prix_max:g} €")
    return None


def verifier_etat(annonce: Annonce, recherche: RechercheEffective) -> Rejet | None:
    if not recherche.etats:
        return None
    if annonce.etat is None:
        return Rejet("etat_inconnu", f"état non reconnu « {annonce.etat_libelle} » alors que des états sont exigés")
    if annonce.etat not in recherche.etats:
        return Rejet("etat", f"état « {annonce.etat} » non souhaité")
    return None


def verifier_vendeur(vendeur: Vendeur | None, veille: Veille) -> Rejet | None:
    """Note minimale, nombre d'avis minimum, pays. Une donnée exigée mais inconnue = rejet (prudence)."""
    if veille.vendeur_note_min is not None:
        if vendeur is None or vendeur.note is None:
            return Rejet("vendeur_note_inconnue", "note vendeur inconnue (aucun avis ?)")
        if vendeur.note < veille.vendeur_note_min:
            return Rejet("vendeur_note", f"note {vendeur.note:g} < {veille.vendeur_note_min:g}")
    if veille.vendeur_avis_min is not None:
        if vendeur is None or vendeur.nb_avis is None:
            return Rejet("vendeur_avis_inconnus", "nombre d'avis du vendeur inconnu")
        if vendeur.nb_avis < veille.vendeur_avis_min:
            return Rejet("vendeur_avis", f"{vendeur.nb_avis} avis < {veille.vendeur_avis_min}")
    if veille.vendeur_pays:
        if vendeur is None or not vendeur.pays:
            return Rejet("vendeur_pays_inconnu", "pays du vendeur inconnu")
        if vendeur.pays not in veille.vendeur_pays:
            return Rejet("vendeur_pays", f"pays {vendeur.pays} hors de {veille.vendeur_pays}")
    return None


def verifier_photos(annonce: Annonce, veille: Veille) -> Rejet | None:
    if veille.photos_min is not None and annonce.nb_photos is not None and annonce.nb_photos < veille.photos_min:
        return Rejet("photos", f"{annonce.nb_photos} photo(s) < minimum {veille.photos_min}")
    return None


def filtrer_avant_detail(annonce: Annonce, veille: Veille, recherche: RechercheEffective, cfg: Config) -> Rejet | None:
    """Filtres gratuits (données du catalogue). Premier rejet rencontré, sinon None."""
    if not annonce.visible:
        return Rejet("invisible", "annonce masquée ou non disponible")
    return (
        verifier_prix(annonce, recherche)
        or verifier_etat(annonce, recherche)
        or verifier_exclusions(f"{annonce.titre} {annonce.marque}", "le titre", veille, cfg)
    )


def filtrer_apres_detail(annonce: Annonce, veille: Veille, cfg: Config) -> Rejet | None:
    """Filtres nécessitant le détail de l'annonce (description, photos, vendeur)."""
    return (
        verifier_exclusions(annonce.description or "", "la description", veille, cfg)
        or verifier_photos(annonce, veille)
        or verifier_vendeur(annonce.vendeur, veille)
    )
