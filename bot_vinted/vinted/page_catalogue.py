"""Lecture de la PAGE de recherche Vinted (`/catalog?search_text=…`) : la liste des annonces y est intégrée.

Depuis que l'ancienne API `/api/v2/catalog/items` répond « La page n'existe pas », le site (Next.js) envoie les
annonces dans le HTML même, sous forme de petits morceaux `self.__next_f.push([1,"…"])` dont la mise bout à bout
donne du JSON. Ce fichier extrait ce JSON et le convertit en `Annonce`. C'est la SEULE partie à adapter si Vinted
change encore la forme de sa page.

Structure observée (page réelle, simplifiée) :
    items: { items: [ { id, productItem: { id, title, url, price{amount,currencyCode}, serviceFee, photos[…],
                         user{id,isBusiness}, itemBox{firstLine: marque, secondLine: "L · Très bon état"} } } ],
             pagination: { current_page, per_page: 96, total_entries, total_pages } }

Ce que la liste NE contient PAS : la date de publication, la description, la note ni le pays du vendeur.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from bot_vinted.vinted.modeles import Annonce, Vendeur, etat_depuis_libelle, lire_montant, _entier, _etat

log = logging.getLogger(__name__)

_MORCEAU = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_LISTE = re.compile(r'"items":\s*(\{\s*"items":\s*\[)')
_SEPARATEUR = " · "


class PageInattendue(ValueError):
    """La page ne contient pas la liste d'annonces attendue (format modifié, page de vérification…)."""


@dataclass
class PageCatalogue:
    annonces: list[Annonce]
    page: int = 1
    pages_total: int | None = None
    total_resultats: int | None = None
    illisibles: int = 0
    brutes: int = 0  # nombre d'éléments présents dans la page, lisibles ou non
    liste: dict = field(default_factory=dict, repr=False)  # objet JSON d'origine (pour la commande `capturer`)


def extraire_flux(html: str) -> str:
    """Remet bout à bout les morceaux de données Next.js de la page."""
    morceaux = []
    for m in _MORCEAU.finditer(html):
        try:
            morceaux.append(json.loads(m.group(1)))
        except ValueError:
            continue  # un morceau illisible n'empêche pas de lire les autres
    return "".join(morceaux)


def extraire_liste(html: str) -> dict:
    """Objet JSON `{items: [...], pagination: {...}}` de la page. Lève PageInattendue s'il est introuvable."""
    flux = extraire_flux(html)
    m = _LISTE.search(flux)
    if not m:
        raise PageInattendue("liste d'annonces introuvable dans la page (format modifié ou page de vérification ?)")
    try:
        objet, _ = json.JSONDecoder().raw_decode(flux, m.start(1))
    except ValueError as e:
        raise PageInattendue(f"liste d'annonces illisible : {e}") from e
    return objet


def _texte(valeur: object) -> str:
    """Texte utile : Next.js écrit « $undefined » pour une valeur absente."""
    if not isinstance(valeur, str):
        return ""
    v = valeur.strip()
    return "" if (not v or v.startswith("$")) else v


def _separer_ligne(ligne: str) -> tuple[str, str]:
    """« L · Très bon état » → (« L », « Très bon état ») ; « Neuf avec étiquette » → (« », « Neuf avec étiquette »)."""
    parties = [p.strip() for p in ligne.split(_SEPARATEUR) if p.strip()]
    if not parties:
        return "", ""
    if len(parties) == 1:
        return ("", parties[0]) if etat_depuis_libelle(parties[0]) else (parties[0], "")
    return _SEPARATEUR.join(parties[:-1]), parties[-1]


def _depuis_etiquette(etiquette: str, cle: str) -> str:
    """Repli : « …, Marque: Carhartt, Taille: L, État: Très bon état » → valeur du champ demandé."""
    m = re.search(rf"{cle}\s*:\s*([^,]+)", etiquette)
    return m.group(1).strip() if m else ""


def analyser_entree(entree: dict, domaine: str) -> Annonce:
    """Un élément de `items.items` → Annonce. Lève ValueError si l'élément est inexploitable."""
    pi = entree.get("productItem") if isinstance(entree.get("productItem"), dict) else entree
    ident = _entier(pi.get("id", entree.get("id")))
    if ident is None:
        raise ValueError("annonce sans identifiant")

    remise = pi.get("priceWithDiscount")
    prix, devise = lire_montant(remise if remise else pi.get("price"))
    if prix is None:
        raise ValueError(f"annonce {ident} sans prix lisible")

    boite = pi.get("itemBox") if isinstance(pi.get("itemBox"), dict) else {}
    etiquette = _texte(boite.get("accessibilityLabel"))
    marque = _texte(boite.get("firstLine")) or _depuis_etiquette(etiquette, "Marque")
    taille, libelle_etat = _separer_ligne(_texte(boite.get("secondLine")))
    taille = taille or _depuis_etiquette(etiquette, "Taille")
    libelle_etat = libelle_etat or _depuis_etiquette(etiquette, "État")
    etat, libelle_etat = _etat({"status": libelle_etat}) if libelle_etat else (None, "")

    photos = pi.get("photos") if isinstance(pi.get("photos"), list) else None
    photo_url = None
    for ph in photos or []:
        if isinstance(ph, dict) and ph.get("url") and (ph.get("isMain") or photo_url is None):
            photo_url = ph["url"]
            if ph.get("isMain"):
                break
    photo_url = photo_url or _texte(pi.get("thumbnailUrl")) or None

    url = _texte(pi.get("url"))
    if url.startswith("/"):
        url = f"https://www.{domaine}{url}"
    elif not url.startswith("https://"):
        url = f"https://www.{domaine}/items/{ident}"

    user = pi.get("user") if isinstance(pi.get("user"), dict) else {}
    vendeur = Vendeur(id=_entier(user.get("id")), pro=bool(user.get("isBusiness"))) if user else None

    return Annonce(
        domaine=domaine, id=ident, titre=_texte(pi.get("title")), url=url, prix=prix, devise=str(devise or "EUR"),
        marque=marque, taille=taille, etat=etat, etat_libelle=libelle_etat, photo_url=photo_url,
        nb_photos=len(photos) if photos is not None else None,
        publie_le=None,  # absente de la liste : le moteur utilise l'instant de première détection
        vendeur=vendeur, favoris=_entier(pi.get("favouriteCount")) or 0, detail_charge=False,
    )


def analyser_page(html: str, domaine: str) -> PageCatalogue:
    """Page de recherche (HTML) → annonces lisibles + pagination. Lève PageInattendue si la liste est introuvable."""
    liste = extraire_liste(html)
    entrees = liste.get("items") if isinstance(liste.get("items"), list) else []
    annonces: list[Annonce] = []
    illisibles = 0
    for entree in entrees:
        try:
            annonces.append(analyser_entree(entree, domaine))
        except (ValueError, TypeError, AttributeError) as e:
            illisibles += 1
            log.warning("Annonce illisible ignorée (%s) : %.120r", e, entree)
    pagination = liste.get("pagination") if isinstance(liste.get("pagination"), dict) else {}
    return PageCatalogue(
        annonces=annonces, page=_entier(pagination.get("current_page")) or 1,
        pages_total=_entier(pagination.get("total_pages")), total_resultats=_entier(pagination.get("total_entries")),
        illisibles=illisibles, brutes=len(entrees),
        # Uniquement annonces + pagination : le reste de l'objet contient des identifiants de session (tracking).
        liste={"items": entrees, "pagination": pagination},
    )
