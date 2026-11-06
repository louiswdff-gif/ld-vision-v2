"""Commande `capturer` : enregistre une VRAIE réponse de Vinted dans un fichier JSON.

Pourquoi : les tests du projet reposent sur des réponses JSON enregistrées (fixtures), jamais sur Vinted en
boucle. Le format réel de Vinted peut différer de ce qu'attend le parseur : cette commande fait UNE requête
(avec la même politesse que le bot : cookie anonyme, pas d'insistance), sauvegarde la réponse brute, puis
la fait lire par le parseur et affiche ce qu'il a compris. Si un champ manque, `vinted/modeles.py` est le
seul fichier à adapter. Le fichier obtenu peut être copié dans tests/fixtures/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from bot_vinted.config import Config, Veille
from bot_vinted.vinted.client import ClientVinted
from bot_vinted.vinted.constantes import normaliser_domaine
from bot_vinted.vinted.modeles import analyser_annonce, analyser_detail


class ErreurCapture(Exception):
    """L'URL fournie n'est pas exploitable pour une capture."""


def _cible(cfg: Config, url: str) -> tuple[str, str, dict[str, str] | None, str]:
    """URL collée → (domaine, chemin API, paramètres, type) avec type ∈ {catalogue, detail, brut}."""
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
    return recherche.domaine, "/api/v2/catalog/items", recherche.parametres_api(1, cfg.reseau.articles_par_page), "catalogue"


async def capturer(cfg: Config, client: ClientVinted, url: str, fichier: Path) -> str:
    """Fait UNE requête, écrit le JSON brut dans `fichier` et retourne un rapport lisible. Peut lever
    ErreurCapture, VintedBloque, ErreurReseau… (l'appelant affiche le message et n'insiste pas)."""
    domaine, chemin, params, type_ = _cible(cfg, url)
    donnees = await client.brut(domaine, chemin, params)
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(donnees, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lignes = [f"Réponse enregistrée : {fichier} ({fichier.stat().st_size} octets) ← {domaine}{chemin}"]
    if type_ == "catalogue":
        lignes += _rapport_catalogue(donnees, domaine)
    elif type_ == "detail":
        lignes += _rapport_detail(donnees, domaine)
    else:
        lignes.append(f"Clés de premier niveau : {', '.join(sorted(donnees)) or '(aucune)'}")
    lignes.append("Pour en faire une fixture de test : copiez ce fichier dans tests/fixtures/.")
    return "\n".join(lignes)


def _rapport_catalogue(donnees: dict, domaine: str) -> list[str]:
    items = donnees.get("items")
    if not isinstance(items, list):
        return ["⚠ Pas de liste `items` dans la réponse : le format a changé ? Clés présentes : "
                + ", ".join(sorted(donnees))]
    lues, illisibles = [], []
    for item in items:
        try:
            lues.append(analyser_annonce(item, domaine))
        except (ValueError, TypeError, AttributeError) as e:
            illisibles.append(str(e))
    lignes = [f"Parseur : {len(lues)} annonce(s) lue(s) sur {len(items)}, {len(illisibles)} illisible(s)."]
    if illisibles:
        lignes.append(f"⚠ Exemple d'erreur : {illisibles[0]} → vérifiez vinted/modeles.py")
    if lues:
        a = lues[0]
        age = a.age_minutes(time.time())
        lignes += [
            "Première annonce telle que le bot la comprend :",
            f"  #{a.id} « {a.titre} » — {a.prix:g} {a.devise} — marque « {a.marque or '(vide)'} » — taille « {a.taille} »",
            f"  état : {a.etat or '(non reconnu)'} (libellé brut « {a.etat_libelle} ») — "
            f"publiée il y a {'?' if age is None else f'{age:.0f} min'} — photo : {'oui' if a.photo_url else 'non'}",
        ]
        manquants = [n for n, ok in (("état", a.etat), ("date de publication", a.publie_le is not None),
                                     ("photo", a.photo_url), ("marque", a.marque), ("taille", a.taille)) if not ok]
        if manquants:
            lignes.append(f"⚠ Champs non extraits sur cette annonce : {', '.join(manquants)} "
                          "(normal si absents de l'annonce ; sinon adaptez vinted/modeles.py).")
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
