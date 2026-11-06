"""Ligne de commande : `python main.py [run] [--dry-run]`, `acheter`, `vendu`, `stats`, `verifier`, `capturer`.

Sans sous-commande, `python main.py` (avec ou sans `--dry-run`) équivaut à `python main.py run`.
"""

from __future__ import annotations

import argparse
import sys

from bot_vinted import __version__
from bot_vinted.config import Config, ErreurConfig, charger_config

COMMANDES = ("run", "verifier", "acheter", "vendu", "stats", "capturer")


def construire_parseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument("--config", default="config.yaml", help="fichier de configuration (défaut : config.yaml)")

    p = argparse.ArgumentParser(
        prog="main.py",
        description="Bot de veille Vinted pour l'achat-revente : détecte, note et alerte. "
        "L'achat reste toujours manuel (via le lien de l'alerte).",
        epilog="Sans sous-commande : lance la surveillance (équivaut à `run`).",
    )
    p.add_argument("--version", action="version", version=f"bot_vinted {__version__}")
    sub = p.add_subparsers(dest="commande", metavar="commande")

    run = sub.add_parser("run", parents=[commun], help="lance la surveillance en continu (défaut)")
    run.add_argument("--dry-run", action="store_true", help="affiche les alertes en console, n'envoie rien")
    run.add_argument("--une-fois", action="store_true", help="un seul passage par veille puis arrêt")
    run.add_argument("-v", "--verbeux", action="store_true", help="logs détaillés (DEBUG)")

    sub.add_parser("verifier", parents=[commun], help="valide config.yaml et affiche un résumé des veilles")

    ach = sub.add_parser("acheter", parents=[commun], help="enregistre un article acheté")
    ach.add_argument("annonce", help="identifiant de l'annonce, ou son URL")
    ach.add_argument("prix", type=_prix, help="TOTAL réellement payé, tout compris (article + protection + port)")
    ach.add_argument("--titre", help="titre (si l'annonce n'a jamais été vue par le bot)")
    ach.add_argument("--veille", help="nom de la veille à créditer (si l'annonce n'a jamais été vue par le bot)")
    ach.add_argument("--domaine", help="domaine Vinted (défaut : celui de l'annonce vue, sinon domaine global)")

    ven = sub.add_parser("vendu", parents=[commun], help="enregistre la revente d'un article")
    ven.add_argument("annonce", help="identifiant de l'annonce, ou son URL")
    ven.add_argument("prix", type=_prix, help="montant NET réellement reçu")
    ven.add_argument("--domaine", help="domaine Vinted (utile si l'identifiant existe sur plusieurs domaines)")

    sub.add_parser("stats", parents=[commun], help="marge réelle, ROI, délai de revente, veilles rentables")

    cap = sub.add_parser(
        "capturer",
        parents=[commun],
        help="enregistre une VRAIE réponse Vinted en JSON (pour créer/valider les fixtures de test)",
    )
    cap.add_argument("url", help="URL de recherche Vinted, URL d'annonce (/items/ID) ou chemin /api/v2/…")
    cap.add_argument("fichier", help="fichier JSON de sortie")
    return p


def _prix(valeur: str) -> float:
    try:
        prix = float(valeur.replace(",", ".").replace("€", "").strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"prix invalide : « {valeur} »") from e
    if prix < 0:
        raise argparse.ArgumentTypeError("le prix ne peut pas être négatif")
    return prix


def _avec_commande_par_defaut(argv: list[str]) -> list[str]:
    """`main.py --dry-run` → `main.py run --dry-run` (sauf demande d'aide ou de version)."""
    if any(a in COMMANDES for a in argv) or any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    return ["run", *argv]


def principal(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = construire_parseur().parse_args(_avec_commande_par_defaut(argv))

    try:
        cfg = charger_config(args.config)
        handler = {
            "run": _cmd_run,
            "verifier": _cmd_verifier,
            "acheter": _cmd_acheter,
            "vendu": _cmd_vendu,
            "stats": _cmd_stats,
            "capturer": _cmd_capturer,
        }[args.commande]
        return handler(cfg, args)
    except ErreurConfig as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------- commandes


def _cmd_verifier(cfg: Config, args: argparse.Namespace) -> int:
    print(f"Configuration valide ({len(cfg.veilles)} veille(s)).")
    print(f"  domaine global      : {cfg.domaine}")
    print(f"  intervalle global   : {cfg.intervalle_secondes} s")
    print(f"  base de données     : {cfg.chemin(cfg.base.chemin)}")
    print(f"  fichier de log      : {cfg.chemin(cfg.journal.fichier)}")
    for v in cfg.veilles:
        print(f"\n● {v.nom}")
        print(f"    intervalle {v.intervalle_secondes} s · marge ≥ {v.marge_min} € · score ≥ {v.score_min}"
              f" · port estimé {v.port_estime} € · revente cible {v.prix_revente_cible or '—'}")
        print(f"    webhook : variable {v.webhook}")
        recherche = cfg.recherche(v)
        print(f"    recherche : {recherche.resume()}")
        for avert in recherche.avertissements:
            print(f"    ⚠ {avert}")
    manquants = cfg.secrets_manquants()
    if manquants:
        print("\nVariables d'environnement absentes de .env (requises hors --dry-run) :")
        for nom in manquants:
            print(f"  - {nom}")
    elif cfg.veilles:
        print("\nSecrets : toutes les variables nécessaires sont définies.")
    if not cfg.veilles:
        print("\nAttention : aucune veille définie (section `veilles:` vide).")
    return 0


def _non_disponible(nom: str) -> int:
    print(f"La commande « {nom} » n'est pas encore disponible dans cette version.", file=sys.stderr)
    return 3


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    return _non_disponible("run")


def _cmd_acheter(cfg: Config, args: argparse.Namespace) -> int:
    return _non_disponible("acheter")


def _cmd_vendu(cfg: Config, args: argparse.Namespace) -> int:
    return _non_disponible("vendu")


def _cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    return _non_disponible("stats")


def _cmd_capturer(cfg: Config, args: argparse.Namespace) -> int:
    return _non_disponible("capturer")
