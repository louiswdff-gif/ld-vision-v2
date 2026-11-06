"""JSON Vinted → objets Python (Annonce, Vendeur).

Tout le « format Vinted » est isolé ICI : si Vinted change son JSON, c'est le seul fichier à corriger.
Le parseur est TOLÉRANT : champs absents ou de forme variable (prix en chaîne OU en objet, états en
libellé OU en identifiant…) ne font jamais planter le bot ; une annonce illisible est ignorée
avec un avertissement dans le journal.

Les formes JSON gérées ont été écrites d'après la connaissance du format public et DOIVENT être
validées sur une vraie réponse : `python main.py capturer <url> <fichier>` enregistre la réponse réelle.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from bot_vinted.texte import normaliser
from bot_vinted.vinted.constantes import IDS_ETATS, LIBELLES_ETATS

log = logging.getLogger(__name__)

_LIBELLES_INCONNUS_VUS: set[str] = set()  # pour n'avertir qu'une fois par libellé


class Vendeur(BaseModel):
    id: int | None = None
    login: str = ""
    note: float | None = None  # sur 5 ; None si inconnue ou aucun avis
    nb_avis: int | None = None
    pays: str | None = None  # code ISO (FR, BE…)
    pro: bool = False


class Annonce(BaseModel):
    domaine: str = ""
    id: int
    titre: str
    url: str
    prix: float  # prix affiché de l'article (hors frais de protection acheteur)
    devise: str = "EUR"
    marque: str = ""  # champ marque de l'annonce (peut être vide ou erroné)
    taille: str = ""
    etat: str | None = None  # clé interne : neuf_avec_etiquette, tres_bon_etat… (None si inconnu)
    etat_libelle: str = ""
    photo_url: str | None = None
    nb_photos: int | None = None  # None tant que le détail n'a pas été chargé
    publie_le: float | None = None  # timestamp Unix (secondes) ; None si inconnu
    categorie_id: int | None = None
    description: str | None = None  # None tant que le détail n'a pas été chargé
    vendeur: Vendeur | None = None
    favoris: int = 0
    vues: int = 0
    visible: bool = True
    detail_charge: bool = False

    def age_minutes(self, maintenant: float) -> float | None:
        """Minutes écoulées depuis la publication (None si la date est inconnue)."""
        if self.publie_le is None:
            return None
        return max(0.0, (maintenant - self.publie_le) / 60)


# --------------------------------------------------------------------------- helpers de lecture tolérante


def _montant(valeur: Any) -> tuple[float | None, str | None]:
    """Prix sous forme de chaîne « 25.0 », de nombre, ou d'objet {amount, currency_code}."""
    devise = None
    if isinstance(valeur, dict):
        devise = valeur.get("currency_code") or valeur.get("currency")
        valeur = valeur.get("amount", valeur.get("value"))
    if valeur is None or isinstance(valeur, bool):
        return None, devise
    try:
        return float(str(valeur).replace(",", ".").strip()), devise
    except ValueError:
        return None, devise


def _horodatage(valeur: Any) -> float | None:
    """Timestamp Unix en secondes, depuis un nombre, une chaîne numérique ou une date ISO 8601."""
    if valeur is None or isinstance(valeur, bool):
        return None
    if isinstance(valeur, (int, float)):
        v = float(valeur)
    else:
        texte = str(valeur).strip()
        try:
            v = float(texte)
        except ValueError:
            try:
                return datetime.fromisoformat(texte.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return v / 1000 if v > 1e11 else v  # certaines API donnent des millisecondes


def _entier(valeur: Any) -> int | None:
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return None


def _etat(item: dict) -> tuple[str | None, str]:
    """(clé interne, libellé brut). Priorité à l'identifiant s'il est fourni, sinon au libellé."""
    libelle = item.get("status")
    if isinstance(libelle, dict):  # forme alternative {id, title}
        sid, libelle = libelle.get("id"), libelle.get("title") or libelle.get("name")
    else:
        sid = item.get("status_id")
    libelle = str(libelle or "")
    cle = IDS_ETATS.get(_entier(sid)) if sid is not None else None
    if cle is None and libelle:
        cle = LIBELLES_ETATS.get(normaliser(libelle))
        if cle is None and libelle not in _LIBELLES_INCONNUS_VUS:
            _LIBELLES_INCONNUS_VUS.add(libelle)
            log.warning("Libellé d'état inconnu « %s » : à ajouter dans vinted/constantes.py (LIBELLES_ETATS).", libelle)
    return cle, libelle


def _photo_principale(item: dict) -> dict | None:
    photo = item.get("photo")
    if isinstance(photo, dict):
        return photo
    photos = item.get("photos")
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        return photos[0]
    return None


def _url_photo(photo: dict | None) -> str | None:
    if not photo:
        return None
    return photo.get("url") or photo.get("full_size_url")


def _date_publication(item: dict, photo: dict | None) -> float | None:
    """Catalogue : timestamp de la photo principale (≈ mise en ligne). Détail : created_at_ts."""
    for brut in (item.get("created_at_ts"), item.get("created_at")):
        t = _horodatage(brut)
        if t:
            return t
    if photo:
        hr = photo.get("high_resolution")
        if isinstance(hr, dict):
            t = _horodatage(hr.get("timestamp"))
            if t:
                return t
        t = _horodatage(photo.get("timestamp"))
        if t:
            return t
    return None


def _marque(item: dict) -> str:
    dto = item.get("brand_dto")
    if isinstance(dto, dict) and dto.get("title"):
        return str(dto["title"])
    return str(item.get("brand_title") or item.get("brand") or "").strip()


def analyser_vendeur(user: dict | None) -> Vendeur | None:
    """Objet `user` d'une annonce ou de /api/v2/users/{id}. La note Vinted est une réputation 0-1 → étoiles sur 5."""
    if not isinstance(user, dict) or not user:
        return None
    nb_avis = _entier(user.get("feedback_count"))
    if nb_avis is None:
        morceaux = [_entier(user.get(k)) for k in ("positive_feedback_count", "neutral_feedback_count", "negative_feedback_count")]
        if any(m is not None for m in morceaux):
            nb_avis = sum(m or 0 for m in morceaux)

    note = user.get("feedback_reputation")
    try:
        note = float(note) if note is not None else None
    except (TypeError, ValueError):
        note = None
    if note is not None and note <= 1:
        note *= 5
    if nb_avis == 0:
        note = None  # aucun avis : une note de 0 serait trompeuse

    pays = user.get("country_iso_code") or user.get("country_code")
    return Vendeur(
        id=_entier(user.get("id")),
        login=str(user.get("login") or ""),
        note=round(note, 2) if note is not None else None,
        nb_avis=nb_avis,
        pays=str(pays).upper() if pays else None,
        pro=bool(user.get("business") or user.get("is_business")),
    )


def analyser_annonce(item: dict, domaine: str) -> Annonce:
    """Un élément de `items` (catalogue) ou l'objet `item` (détail) → Annonce. Lève ValueError si illisible."""
    ident = _entier(item.get("id"))
    if ident is None:
        raise ValueError("annonce sans identifiant")
    prix, devise = _montant(item.get("price"))
    if prix is None:
        raise ValueError(f"annonce {ident} sans prix lisible")
    devise = devise or item.get("currency") or "EUR"

    photo = _photo_principale(item)
    photos = item.get("photos")
    etat, libelle = _etat(item)
    url = item.get("url") or ""
    if not url:
        chemin = item.get("path") or f"/items/{ident}"
        url = f"https://www.{domaine}{chemin}"

    vendeur = analyser_vendeur(item.get("user"))
    if vendeur and not vendeur.pays and item.get("country_iso_code"):
        vendeur.pays = str(item["country_iso_code"]).upper()

    return Annonce(
        domaine=domaine,
        id=ident,
        titre=str(item.get("title") or "").strip(),
        url=url,
        prix=prix,
        devise=str(devise),
        marque=_marque(item),
        taille=str(item.get("size_title") or item.get("size") or "").strip(),
        etat=etat,
        etat_libelle=libelle,
        photo_url=_url_photo(photo),
        nb_photos=len(photos) if isinstance(photos, list) else None,
        publie_le=_date_publication(item, photo),
        categorie_id=_entier(item.get("catalog_id")),
        description=item.get("description") if isinstance(item.get("description"), str) else None,
        vendeur=vendeur,
        favoris=_entier(item.get("favourite_count")) or 0,
        vues=_entier(item.get("view_count")) or 0,
        visible=item.get("is_visible", True) is not False,
        detail_charge=isinstance(item.get("description"), str) and isinstance(photos, list),
    )


def analyser_catalogue(donnees: dict, domaine: str) -> list[Annonce]:
    """Réponse de /api/v2/catalog/items → liste d'annonces (les éléments illisibles sont ignorés)."""
    annonces: list[Annonce] = []
    for item in donnees.get("items") or []:
        try:
            annonces.append(analyser_annonce(item, domaine))
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("Annonce illisible ignorée (%s) : %.120r", e, item)
    return annonces


def analyser_detail(donnees: dict, domaine: str) -> Annonce:
    """Réponse de /api/v2/items/{id} → Annonce complète (description, photos, vendeur)."""
    item = donnees.get("item") if isinstance(donnees.get("item"), dict) else donnees
    return analyser_annonce(item, domaine)


def completer(annonce: Annonce, detail: Annonce) -> Annonce:
    """Enrichit une annonce du catalogue avec les informations du détail (sans perdre celles déjà connues)."""
    maj = {
        "description": detail.description,
        "nb_photos": detail.nb_photos,
        "detail_charge": True,
    }
    if detail.vendeur:
        maj["vendeur"] = detail.vendeur if not annonce.vendeur else _fusionner_vendeur(annonce.vendeur, detail.vendeur)
    if detail.categorie_id is not None:
        maj["categorie_id"] = detail.categorie_id
    if not annonce.marque and detail.marque:
        maj["marque"] = detail.marque
    if annonce.publie_le is None and detail.publie_le is not None:
        maj["publie_le"] = detail.publie_le
    if not annonce.photo_url and detail.photo_url:
        maj["photo_url"] = detail.photo_url
    return annonce.model_copy(update=maj)


def _fusionner_vendeur(connu: Vendeur, nouveau: Vendeur) -> Vendeur:
    """Le détail complète le vendeur du catalogue : on garde chaque valeur disponible."""
    return Vendeur(
        id=nouveau.id if nouveau.id is not None else connu.id,
        login=nouveau.login or connu.login,
        note=nouveau.note if nouveau.note is not None else connu.note,
        nb_avis=nouveau.nb_avis if nouveau.nb_avis is not None else connu.nb_avis,
        pays=nouveau.pays or connu.pays,
        pro=nouveau.pro or connu.pro,
    )
