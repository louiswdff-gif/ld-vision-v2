"""Ligne de commande : `python main.py [run] [--dry-run]`, `acheter`, `vendu`, `stats`, `verifier`, `capturer`.

Sans sous-commande, `python main.py` (avec ou sans `--dry-run`) équivaut à `python main.py run`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from bot_vinted import __version__
from bot_vinted.config import Config, ErreurConfig, charger_config

COMMANDES = ("run", "verifier", "acheter", "vendu", "stats", "capturer", "tester", "telegram-id", "marques-ids")


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
    ids = sub.add_parser("marques-ids", parents=[commun],
                         help="retrouve les identifiants Vinted des marques de marques.yaml (recherches multi-marques)")
    ids.add_argument("--marques", help="liste restreinte, séparée par des virgules (défaut : celles de marques.yaml "
                                       "sauf `scanner: false`)")
    ids.add_argument("--toutes", action="store_true", help="inclut aussi les marques marquées `scanner: false`")
    sub.add_parser("tester", parents=[commun],
                   help="envoie un message et une alerte D'ESSAI sur les canaux activés (Discord, Telegram)")
    sub.add_parser("telegram-id", parents=[commun],
                   help="trouve votre identifiant de conversation Telegram (TELEGRAM_CHAT_ID)")

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
            "tester": _cmd_tester,
            "telegram-id": _cmd_telegram_id,
            "marques-ids": _cmd_marques_ids,
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


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    import asyncio

    from bot_vinted.journal import configurer_journal
    from bot_vinted.marques import CatalogueMarques

    if not cfg.veilles:
        raise ErreurConfig("Aucune veille définie : ajoutez au moins une entrée dans la section `veilles:` de config.yaml.")
    manquants = cfg.secrets_manquants()
    if manquants and not args.dry_run:
        raise ErreurConfig(
            "Variables d'environnement manquantes (à définir dans .env) : " + ", ".join(manquants)
            + "\nAstuce : `python main.py --dry-run` permet de tester sans aucun secret (alertes en console)."
        )
    configurer_journal(cfg, verbeux=args.verbeux)
    marques = CatalogueMarques.charger(cfg.chemin(cfg.marques.fichier), cfg.marques)
    asyncio.run(_surveiller(cfg, marques, dry_run=args.dry_run, une_fois=args.une_fois))
    return 0


async def _surveiller(cfg: Config, marques, *, dry_run: bool, une_fois: bool) -> None:
    from bot_vinted.alertes import construire_diffuseur
    from bot_vinted.base import Base
    from bot_vinted.moteur import Moteur
    from bot_vinted.vinted.client import ClientVinted

    base = Base(cfg.chemin(cfg.base.chemin))
    client = ClientVinted(cfg.reseau)
    bot = None
    diffuseur = None
    try:
        if cfg.discord_bot.actif and dry_run:
            logging.getLogger(__name__).info("Mode --dry-run : le bot Discord n'est pas démarré (alertes en console).")
        elif cfg.discord_bot.actif:
            try:
                from bot_vinted.discord_bot.bot import BotDiscord
            except ImportError as e:
                raise ErreurConfig("La bibliothèque discord.py est requise par `discord_bot.actif` : "
                                   "pip install -r requirements.txt") from e
            bot = BotDiscord(cfg, base)
            await bot.demarrer()  # lève ErreurConfig si le token est refusé
        diffuseur = construire_diffuseur(cfg, dry_run, bot)
        await Moteur(cfg, base, client, diffuseur, marques, dry_run=dry_run).lancer(une_fois=une_fois)
    finally:
        await client.fermer()
        if diffuseur is not None:
            await diffuseur.fermer()
        if bot is not None:
            await bot.arreter()
        base.fermer()


def _cmd_acheter(cfg: Config, args: argparse.Namespace) -> int:
    from bot_vinted.base import Base
    from bot_vinted.suivi import ErreurSuivi, acheter

    base = Base(cfg.chemin(cfg.base.chemin))
    try:
        print(acheter(base, args.annonce, args.prix, time.time(), domaine_defaut=cfg.domaine, titre=args.titre,
                      veille=args.veille, domaine=args.domaine))
        return 0
    except (ErreurSuivi, ValueError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    finally:
        base.fermer()


def _cmd_vendu(cfg: Config, args: argparse.Namespace) -> int:
    from bot_vinted.base import Base
    from bot_vinted.suivi import ErreurSuivi, vendre

    base = Base(cfg.chemin(cfg.base.chemin))
    try:
        print(vendre(base, args.annonce, args.prix, time.time(), domaine_defaut=cfg.domaine, domaine=args.domaine))
        return 0
    except (ErreurSuivi, ValueError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    finally:
        base.fermer()


def _cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    from bot_vinted.base import Base
    from bot_vinted.suivi import calculer_stats, formater_stats

    chemin = cfg.chemin(cfg.base.chemin)
    achats = []
    if chemin.is_file():  # ne crée pas une base vide juste pour afficher des statistiques
        with Base(chemin) as base:
            achats = base.tous_les_achats()
    print(formater_stats(calculer_stats(achats)))
    return 0


def _cmd_capturer(cfg: Config, args: argparse.Namespace) -> int:
    import asyncio
    from pathlib import Path

    from bot_vinted.capture import ErreurCapture, capturer
    from bot_vinted.vinted.client import ClientVinted, ErreurVinted, VintedBloque

    async def _faire() -> int:
        client = ClientVinted(cfg.reseau)
        try:
            print(await capturer(cfg, client, args.url, Path(args.fichier)))
            return 0
        except ErreurCapture as e:
            print(f"Erreur : {e}", file=sys.stderr)
            return 1
        except VintedBloque as e:  # jamais d'insistance : on s'arrête et on le dit
            print(f"Vinted a refusé la requête ({e.motif}). Réessayez plus tard, depuis votre connexion habituelle ; "
                  "le bot ne contourne aucune protection.", file=sys.stderr)
            return 2
        except ErreurVinted as e:
            print(f"Erreur Vinted : {e}", file=sys.stderr)
            return 2
        finally:
            await client.fermer()

    return asyncio.run(_faire())


def _cmd_telegram_id(cfg: Config, args: argparse.Namespace) -> int:
    import asyncio

    from bot_vinted.alertes import ErreurEnvoi
    from bot_vinted.alertes import telegram as module_telegram

    token = Config.secret(cfg.telegram.token_env)
    if not token:
        raise ErreurConfig(f"Variable {cfg.telegram.token_env} absente de .env : collez-y le token donné par @BotFather.")
    try:
        conversations = asyncio.run(module_telegram.decouvrir_conversations(token))
    except ErreurEnvoi as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    if not conversations:
        print("Aucune conversation trouvée.\nOuvrez votre bot dans Telegram, envoyez-lui un message (par exemple "
              "« salut »), puis relancez cette commande.", file=sys.stderr)
        return 1
    print("Conversation(s) trouvée(s) :")
    for ident, libelle in conversations:
        print(f"  {libelle} → TELEGRAM_CHAT_ID={ident}")
    print("\nCopiez la ligne voulue dans votre fichier .env, puis lancez : python main.py tester")
    return 0


def _alerte_d_essai(cfg: Config):
    from bot_vinted.alertes import Alerte

    return Alerte(
        domaine=cfg.domaine, annonce_id=0, veille="ESSAI", titre="[ESSAI] Veste Carhartt Detroit (annonce fictive)",
        url=f"https://www.{cfg.domaine}/", prix=28.0, cout_reel=36.6, revente_estimee=70.0, source_revente="cible",
        nb_references=0, marge=33.4, roi_pct=91.3, score=87, palier="🔥", marque="Carhartt", taille="L",
        etat="tres_bon_etat", tags=["🧪 Alerte d'essai : aucune annonce réelle, rien à acheter"], publie_le=time.time(),
    )


def _cmd_tester(cfg: Config, args: argparse.Namespace) -> int:
    import asyncio

    manquants = cfg.secrets_manquants()
    if manquants:
        raise ErreurConfig("Variables d'environnement manquantes (à définir dans .env) : " + ", ".join(manquants))
    return asyncio.run(_essayer(cfg))


async def _essayer(cfg: Config) -> int:
    """Envoie un message système puis une alerte fictive sur chaque canal activé ; affiche le résultat de chacun."""
    from bot_vinted.alertes import ErreurEnvoi, construire_diffuseur
    from bot_vinted.base import Base

    bot = base = diffuseur = None
    try:
        if cfg.discord_bot.actif:
            try:
                from bot_vinted.discord_bot.bot import BotDiscord
            except ImportError as e:
                raise ErreurConfig("La bibliothèque discord.py est requise : pip install -r requirements.txt") from e
            base = Base(":memory:")
            bot = BotDiscord(cfg, base)
            await bot.demarrer()
        diffuseur = construire_diffuseur(cfg, False, bot)
        tout_va_bien = True
        for canal in diffuseur.canaux:
            try:
                await canal.envoyer_texte("✅ Test du bot de veille Vinted : si vous lisez ceci, ce canal fonctionne.")
                await canal.envoyer_alerte(_alerte_d_essai(cfg), time.time())
                print(f"✓ {canal.nom} : message et alerte d'essai envoyés.")
            except ErreurEnvoi as e:
                tout_va_bien = False
                print(f"✗ {canal.nom} : {e}", file=sys.stderr)
        return 0 if tout_va_bien else 1
    finally:
        if diffuseur is not None:
            await diffuseur.fermer()
        if bot is not None:
            await bot.arreter()
        if base is not None:
            base.fermer()


def _cmd_marques_ids(cfg: Config, args: argparse.Namespace) -> int:
    import asyncio

    import yaml

    from bot_vinted.vinted.client import ClientVinted, ErreurVinted
    from bot_vinted.vinted.marques_ids import extraire_ids, resoudre
    from bot_vinted.vinted.page_catalogue import extraire_flux

    if args.marques:
        noms = [m.strip() for m in args.marques.split(",") if m.strip()]
    else:
        chemin = cfg.chemin(cfg.marques.fichier)
        if not chemin.is_file():
            raise ErreurConfig(f"Fichier de marques introuvable : {chemin}")
        definitions = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
        noms = [str(n) for n, d in definitions.items()
                if args.toutes or not (isinstance(d, dict) and d.get("scanner") is False)]

    async def _lire() -> str:
        client = ClientVinted(cfg.reseau)
        try:
            html = await client.texte(cfg.domaine, "/brands")
            return html + extraire_flux(html)
        finally:
            await client.fermer()

    try:
        index = extraire_ids(asyncio.run(_lire()))
    except ErreurVinted as e:  # blocage ou panne : on s'arrête, sans insister
        print(f"Vinted n'a pas répondu normalement ({e}). Réessayez plus tard.", file=sys.stderr)
        return 2
    if not index:
        print("La page des marques ne contient aucun identifiant lisible : le format a peut-être changé.", file=sys.stderr)
        return 2
    trouvees, manquantes = resoudre(noms, index)
    print(f"{len(index)} marques connues de Vinted ; {len(trouvees)} trouvée(s) sur {len(noms)} demandée(s).\n")
    for nom, ident in trouvees.items():
        print(f"  {ident:>8}  {nom}")
    if manquantes:
        print("\nIntrouvables sur la page (cochez-les dans le filtre « Marque » de vinted.fr et copiez l'adresse) :")
        print("  " + ", ".join(manquantes))
    print("\nÀ coller dans une veille (config.yaml) :")
    print(f"    marques: [{', '.join(str(i) for i in trouvees.values())}]")
    print("    # " + ", ".join(trouvees))
    return 0
