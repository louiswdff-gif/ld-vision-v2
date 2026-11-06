"""URL de recherche Vinted collée par l'utilisateur → paramètres de recherche.

Exemple : https://www.vinted.fr/catalog?search_text=carhartt%20veste&brand_ids[]=362&price_to=40&status_ids[]=6
donne : texte « carhartt veste », marque 362, prix max 40, état « neuf_avec_etiquette ».

Principe important : seuls les filtres d'IDENTITÉ de l'article (texte, marques, catégories, tailles,
couleurs, matières) sont envoyés à l'API. Les prix et les états sont appliqués par le bot APRÈS
l'enregistrement dans l'historique : sinon l'historique ne contiendrait que des annonces bon marché
et la médiane de revente serait biaisée vers le bas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from bot_vinted.vinted.constantes import IDS_ETATS, normaliser_domaine

if TYPE_CHECKING:  # évite un import circulaire (config importe ce module pour valider les URL)
    from bot_vinted.config import Veille

# Nom du paramètre (avec ou sans « [] ») → champ. Vinted utilise des variantes selon les pages.
_LISTES_IDS = {
    "brand_ids": "marques",
    "brand_id": "marques",
    "catalog": "categories",
    "catalog_ids": "categories",
    "size_ids": "tailles",
    "size_id": "tailles",
    "color_ids": "couleurs",
    "color_id": "couleurs",
    "material_ids": "materiaux",
    "material_id": "materiaux",
    "status_ids": "statuts",
    "status": "statuts",
}
# Paramètres connus mais volontairement sans effet (pagination, tracking, tri géré par le bot…).
_IGNORES_SILENCIEUX = {
    "order", "page", "per_page", "currency", "time", "search_id", "disabled_personalization",
    "catalog_from", "search_by_image_uuid", "created_at", "referrer", "homepage_session_id",
}


class UrlRechercheInvalide(ValueError):
    """L'URL ne ressemble pas à une recherche Vinted exploitable."""


@dataclass
class ParametresRecherche:
    """Paramètres extraits d'une URL de recherche (avant fusion avec les champs YAML)."""

    domaine: str | None = None
    texte: str = ""
    marques: list[int] = field(default_factory=list)
    categories: list[int] = field(default_factory=list)
    tailles: list[int] = field(default_factory=list)
    couleurs: list[int] = field(default_factory=list)
    materiaux: list[int] = field(default_factory=list)
    etats: list[str] = field(default_factory=list)
    prix_min: float | None = None
    prix_max: float | None = None
    avertissements: list[str] = field(default_factory=list)


@dataclass
class RechercheEffective:
    """Recherche finale d'une veille : URL + champs YAML explicites (qui ont priorité)."""

    domaine: str
    texte: str
    marques: list[int]
    categories: list[int]
    tailles: list[int]
    couleurs: list[int]
    materiaux: list[int]
    etats: list[str]
    prix_min: float | None
    prix_max: float | None
    avertissements: list[str] = field(default_factory=list)

    def parametres_api(self, page: int = 1, par_page: int = 48) -> dict[str, str]:
        """Paramètres de `/api/v2/catalog/items`. Tri toujours « plus récentes d'abord »."""
        p: dict[str, str] = {"order": "newest_first", "page": str(page), "per_page": str(par_page)}
        if self.texte:
            p["search_text"] = self.texte
        for cle, valeurs in (
            ("brand_ids", self.marques),
            ("catalog_ids", self.categories),
            ("size_ids", self.tailles),
            ("color_ids", self.couleurs),
            ("material_ids", self.materiaux),
        ):
            if valeurs:
                p[cle] = ",".join(str(v) for v in valeurs)
        return p

    def variante(self, texte: str) -> "RechercheEffective":
        """Recherche par variante orthographique de marque : autre texte, et SANS filtre de marque.

        Le but est justement de trouver les annonces dont le champ marque est vide ou erroné : un filtre
        `brand_ids` les écarterait."""
        from dataclasses import replace

        return replace(self, texte=texte, marques=[])

    def resume(self) -> str:
        """Description lisible pour `python main.py verifier`."""
        morceaux = [f"domaine {self.domaine}", f"texte « {self.texte} »" if self.texte else "sans texte"]
        for nom, valeurs in (
            ("marques", self.marques),
            ("catégories", self.categories),
            ("tailles", self.tailles),
            ("couleurs", self.couleurs),
            ("matières", self.materiaux),
            ("états", self.etats),
        ):
            if valeurs:
                morceaux.append(f"{nom} {valeurs}")
        if self.prix_min is not None or self.prix_max is not None:
            morceaux.append(f"prix {self.prix_min if self.prix_min is not None else '…'}–"
                            f"{self.prix_max if self.prix_max is not None else '…'} €")
        return " · ".join(morceaux)


def _entiers(valeurs: list[str], nom: str, avertissements: list[str]) -> list[int]:
    """« 1,2 » ou ["1", "2"] → [1, 2] (dédoublonné, ordre conservé)."""
    sortie: list[int] = []
    for brut in valeurs:
        for morceau in brut.split(","):
            morceau = morceau.strip()
            if not morceau:
                continue
            try:
                n = int(morceau)
            except ValueError:
                avertissements.append(f"{nom} : valeur non numérique ignorée « {morceau} »")
                continue
            if n not in sortie:
                sortie.append(n)
    return sortie


def _prix(valeurs: list[str], nom: str, avertissements: list[str]) -> float | None:
    if not valeurs:
        return None
    try:
        return float(valeurs[-1].replace(",", "."))
    except ValueError:
        avertissements.append(f"{nom} : valeur invalide ignorée « {valeurs[-1]} »")
        return None


def analyser_url(url: str) -> ParametresRecherche:
    """Extrait les paramètres d'une URL de recherche Vinted. Lève UrlRechercheInvalide si inexploitable."""
    brut = url.strip()
    if not brut:
        raise UrlRechercheInvalide("URL de recherche vide")
    if "://" not in brut:
        brut = "https://" + brut
    morceaux = urlsplit(brut)
    try:
        domaine = normaliser_domaine(morceaux.netloc)
    except ValueError as e:
        raise UrlRechercheInvalide(f"URL de recherche : {e}") from e

    resultat = ParametresRecherche(domaine=domaine)
    avert = resultat.avertissements

    # Catégorie dans le chemin : /catalog/2050-jackets ou /catalog/2050-jackets/…
    segments = [s for s in morceaux.path.split("/") if s]
    if segments and segments[0] == "catalog":
        for seg in segments[1:]:
            tete = seg.split("-", 1)[0]
            if tete.isdigit():
                resultat.categories.append(int(tete))
                break
    elif segments:
        avert.append(f"chemin « /{segments[0]} » inattendu : une URL de recherche commence par /catalog")

    requete = parse_qs(morceaux.query, keep_blank_values=False)
    statuts: list[int] = []
    inconnus: list[str] = []
    for cle_brute, valeurs in requete.items():
        cle = cle_brute.removesuffix("[]")
        if cle == "search_text":
            resultat.texte = " ".join(valeurs[-1].split())
        elif cle == "price_from":
            resultat.prix_min = _prix(valeurs, cle, avert)
        elif cle == "price_to":
            resultat.prix_max = _prix(valeurs, cle, avert)
        elif cle in _LISTES_IDS:
            champ = _LISTES_IDS[cle]
            ids = _entiers(valeurs, cle, avert)
            if champ == "statuts":
                statuts.extend(ids)
            else:
                cible = getattr(resultat, champ)
                cible.extend(i for i in ids if i not in cible)
        elif cle in _IGNORES_SILENCIEUX:
            if cle == "order" and valeurs[-1] != "newest_first":
                avert.append(f"tri « {valeurs[-1]} » remplacé par newest_first (le bot surveille les nouveautés)")
        else:
            inconnus.append(cle_brute)

    for s in statuts:
        if s in IDS_ETATS:
            if IDS_ETATS[s] not in resultat.etats:
                resultat.etats.append(IDS_ETATS[s])
        else:
            avert.append(f"état Vinted inconnu (status_ids={s}) ignoré")
    if inconnus:
        avert.append(f"paramètres non pris en charge, ignorés : {', '.join(sorted(inconnus))}")
    return resultat


def recherche_de_la_veille(veille: "Veille", domaine_global: str) -> RechercheEffective:
    """Fusionne l'URL de la veille et ses champs YAML explicites (qui l'emportent)."""
    base = analyser_url(veille.url_recherche) if veille.url_recherche else ParametresRecherche()

    def choisir(explicite, depuis_url):
        return list(explicite) if explicite else list(depuis_url)

    return RechercheEffective(
        domaine=veille.domaine or base.domaine or domaine_global,
        texte=" ".join(veille.mots_cles) if veille.mots_cles else base.texte,
        marques=choisir(veille.marques, base.marques),
        categories=choisir(veille.categories, base.categories),
        tailles=choisir(veille.tailles, base.tailles),
        couleurs=choisir(veille.couleurs, base.couleurs),
        materiaux=choisir(veille.matieres, base.materiaux),
        etats=choisir(veille.etats, base.etats),
        prix_min=veille.prix_min if veille.prix_min is not None else base.prix_min,
        prix_max=veille.prix_max if veille.prix_max is not None else base.prix_max,
        avertissements=list(base.avertissements),
    )
