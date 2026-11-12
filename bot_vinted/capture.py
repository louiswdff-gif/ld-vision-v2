"""Commande `capturer` : enregistre une VRAIE réponse de Vinted dans un fichier JSON.

Pourquoi : les tests du projet reposent sur des réponses JSON enregistrées (fixtures), jamais sur Vinted en
boucle. Le format réel de Vinted peut différer de ce qu'attend le parseur : cette commande fait UNE requête
(avec la même politesse que le bot : cookie anonyme, pas d'insistance), sauvegarde la réponse brute, puis
la fait lire par le parseur et affiche ce qu'il a compris. Si un champ manque, `vinted/modeles.py` est le
seul fichier à adapter. Le fichier obtenu peut être copié dans tests/fixtures/.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from bot_vinted.config import Config, Veille
from bot_vinted.vinted.client import ClientVinted
from bot_vinted.vinted.constantes import normaliser_domaine
from bot_vinted.vinted.modeles import analyser_detail
from bot_vinted.vinted.page_catalogue import PageInattendue, analyser_page


class ErreurCapture(Exception):
    """L'URL fournie n'est pas exploitable pour une capture."""


def _cible(cfg: Config, url: str) -> tuple[str, str, list[tuple[str, str]] | dict[str, str] | None, str]:
    """URL collée → (domaine, chemin, paramètres, type) avec type ∈ {catalogue, detail, brut}."""
    brut = url.strip()
    if "://" not in brut and brut.startswith("/api/"):
        return cfg.domaine, brut.split("?", 1)[0], dict(parse_qsl(urlsplit(brut).query)) or None, "brut"
    if "://" not in brut:
        brut = "https://" + brut
    morceaux = urlsplit(brut)
    try:
        domaine = normaliser_domaine(morceaux.netloc)
    except ValueError as e:
        raise ErreurCapture(str(e)) from e
    if morceaux.path.startswith("/api/"):
        return domaine, morceaux.path, dict(parse_qsl(morceaux.query)) or None, "brut"
    if "/items/" in morceaux.path:
        ident = morceaux.path.split("/items/", 1)[1].split("-", 1)[0].strip("/")
        if not ident.isdigit():
            raise ErreurCapture(f"identifiant d'annonce introuvable dans {url}")
        return domaine, f"/api/v2/items/{ident}", None, "detail"
    veille = Veille(nom="capture", url_recherche=brut)
    recherche = cfg.recherche(veille)
    return recherche.domaine, "/catalog", recherche.parametres_page(1), "catalogue"


async def capturer(cfg: Config, client: ClientVinted, url: str, fichier: Path) -> str:
    """Fait UNE requête, écrit le JSON dans `fichier` et retourne un rapport lisible. Peut lever
    ErreurCapture, VintedBloque, ErreurReseau… (l'appelant affiche le message et n'insiste pas).

    Pour une recherche, on lit la PAGE web et on n'enregistre que la liste d'annonces qu'elle contient (quelques
    centaines de Ko, sans cookies ni jetons) ; pour les autres cibles, la réponse JSON brute de l'API."""
    domaine, chemin, params, type_ = _cible(cfg, url)
    if type_ == "catalogue":
        html = await client.texte(domaine, chemin, params)
        try:
            page = analyser_page(html, domaine)
        except PageInattendue as e:
            return f"⚠ Page reçue ({len(html)} caractères) mais inexploitable : {e}"
        donnees = page.liste
    else:
        donnees = await client.brut(domaine, chemin, params)
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(donnees, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lignes = [f"Réponse enregistrée : {fichier} ({fichier.stat().st_size} octets) ← {domaine}{chemin}"]
    if type_ == "catalogue":
        lignes += _rapport_catalogue(page)
    elif type_ == "detail":
        lignes += _rapport_detail(donnees, domaine)
    else:
        lignes.append(f"Clés de premier niveau : {', '.join(sorted(donnees)) or '(aucune)'}")
    lignes.append("Pour en faire une fixture de test : copiez ce fichier dans tests/fixtures/.")
    return "\n".join(lignes)


def _rapport_catalogue(page) -> list[str]:
    lignes = [f"Parseur : {len(page.annonces)} annonce(s) lue(s) sur {page.brutes}, {page.illisibles} illisible(s) "
              f"(page {page.page}/{page.pages_total or '?'}, {page.total_resultats or '?'} résultats au total)."]
    if page.illisibles:
        lignes.append("⚠ Des annonces n'ont pas pu être lues : vérifiez vinted/page_catalogue.py")
    if page.annonces:
        a = page.annonces[0]
        lignes += [
            "Première annonce telle que le bot la comprend :",
            f"  #{a.id} « {a.titre} » — {a.prix:g} {a.devise} — marque « {a.marque or '(vide)'} » — taille « {a.taille} »",
            f"  état : {a.etat or '(non reconnu)'} (libellé brut « {a.etat_libelle} ») — "
            f"{a.nb_photos if a.nb_photos is not None else '?'} photo(s) — vendeur #{a.vendeur.id if a.vendeur else '?'}"
            f"{' (professionnel)' if a.vendeur and a.vendeur.pro else ''}",
        ]
        manquants = [n for n, ok in (("état", a.etat), ("photo", a.photo_url), ("marque", a.marque),
                                     ("taille", a.taille)) if not ok]
        if manquants:
            lignes.append(f"⚠ Champs non extraits sur cette annonce : {', '.join(manquants)} "
                          "(normal s'ils sont absents de l'annonce ; sinon adaptez vinted/page_catalogue.py).")
    lignes.append("Note : la date de publication, la description et la note du vendeur ne figurent pas dans la liste.")
    return lignes


def _rapport_detail(donnees: dict, domaine: str) -> list[str]:
    try:
        a = analyser_detail(donnees, domaine)
    except (ValueError, TypeError, AttributeError) as e:
        return [f"⚠ Détail illisible pour le parseur : {e} → vérifiez vinted/modeles.py"]
    v = a.vendeur
    return [
        "Détail tel que le bot le comprend :",
        f"  #{a.id} « {a.titre} » — description : {len(a.description or '')} caractère(s) — "
        f"{a.nb_photos if a.nb_photos is not None else '?'} photo(s) — catégorie {a.categorie_id or '?'}",
        "  vendeur : " + (f"{v.login} — note {v.note if v.note is not None else '?'} — "
                          f"{v.nb_avis if v.nb_avis is not None else '?'} avis — pays {v.pays or '?'}" if v else "(absent)"),
    ]
