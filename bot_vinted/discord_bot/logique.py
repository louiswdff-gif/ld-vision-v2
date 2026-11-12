"""Contenu et règles du bot Discord, sans dépendance à discord.py.

Les « embeds » sont des dictionnaires au format JSON de Discord (title, description, color, fields, footer) :
discord.py les convertit avec `discord.Embed.from_dict`. Tout texte venant de Vinted (titres, noms…) est
échappé avant d'être mis en forme, pour qu'une annonce ne puisse ni casser la mise en page ni fabriquer
un faux lien dans un message du bot.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from bot_vinted.alertes.base import euros, pourcentage, temps_ecoule
from bot_vinted.base import Base
from bot_vinted.config import Config
from bot_vinted.score import palier
from bot_vinted.suivi import (
    SANS_VEILLE,
    ErreurSuivi,
    acheter,
    calculer_stats,
    conseil_calibrage,
    vendre,
)

HEURE = 3600
JOUR = 86400
PREFIXE_BOUTON = "vt"
ACTIONS_BOUTON = ("achete", "vendu")
COULEUR_STATS, COULEUR_VEILLES, COULEUR_TOP = 0x3498DB, 0x9B59B6, 0xFF5722
COULEUR_ACTIF, COULEUR_PAUSE, COULEUR_INACTIF = 0x2ECC71, 0xE67E22, 0x95A5A6
PRIX_MAX = 100_000.0


# --------------------------------------------------------------------------- utilitaires


def echapper_markdown(texte: str, longueur_max: int = 80) -> str:
    """Neutralise le Markdown Discord (liens, gras, citations, mentions…) d'un texte venant de Vinted."""
    texte = " ".join(str(texte).split())
    if len(texte) > longueur_max:
        texte = texte[: longueur_max - 1] + "…"
    texte = re.sub(r"([\\*_~`|>\[\]()#:<])", r"\\\1", texte)  # « : » (faux emojis) et « < » (mentions) compris
    return texte.replace("@", "@​")  # espace de largeur nulle : @everyone / @here ne peuvent plus mentionner


def autorise(cfg: Config, utilisateur_id: int) -> bool:
    """Seuls les utilisateurs listés dans `discord_bot.utilisateurs_autorises` peuvent enregistrer achats et ventes."""
    return utilisateur_id in cfg.discord_bot.utilisateurs_autorises


def analyser_prix(texte: str) -> float:
    """« 36,10 », « 36.10 € », « 36 » → 36.10. Lève ValueError avec un message lisible si le prix est invalide."""
    brut = texte.replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".").strip()
    try:
        prix = float(brut)
    except ValueError:
        raise ValueError(f"« {texte.strip()} » n'est pas un prix valide (exemple : 36,10).") from None
    if not math.isfinite(prix) or prix < 0 or prix > PRIX_MAX:
        raise ValueError(f"Le prix doit être compris entre 0 et {PRIX_MAX:g} €.")
    return prix


def id_bouton(action: str, domaine: str, annonce_id: int) -> str:
    """Identifiant (custom_id) d'un bouton d'alerte : « vt:achete:vinted.fr:4001 » (moins de 100 caractères)."""
    if action not in ACTIONS_BOUTON:
        raise ValueError(f"action de bouton inconnue : {action}")
    return f"{PREFIXE_BOUTON}:{action}:{domaine}:{annonce_id}"


def analyser_id_bouton(custom_id: str) -> tuple[str, str, int] | None:
    """Inverse de `id_bouton` ; None si l'identifiant n'est pas un des nôtres."""
    m = re.fullmatch(rf"{PREFIXE_BOUTON}:({'|'.join(ACTIONS_BOUTON)}):(vinted\.[a-z.]+):(\d+)", custom_id)
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _lien(titre: str, url: str) -> str:
    """Lien Markdown sûr : le titre est échappé, et l'URL doit être en https (sinon, simple texte)."""
    if not url.startswith("https://") or any(c in url for c in " ()<>\\"):
        return echapper_markdown(titre)
    return f"[{echapper_markdown(titre)}]({url})"


# --------------------------------------------------------------------------- /top


def embed_top(base: Base, cfg: Config, maintenant: float, heures: int = 24, nombre: int = 5) -> dict[str, Any]:
    heures = max(1, min(int(heures), 168))
    nombre = max(1, min(int(nombre), 10))
    lignes = base.meilleures_annonces(maintenant - heures * HEURE, nombre)
    embed: dict[str, Any] = {"title": f"🏆 Meilleures affaires des {heures} dernières heures", "color": COULEUR_TOP}
    if not lignes:
        embed["description"] = "Aucune annonce retenue sur cette période."
        return embed
    morceaux = []
    for i, a in enumerate(lignes, 1):
        quand = temps_ecoule(a["publie_le"] or a["cree_le"], maintenant)
        morceaux.append(
            f"**{i}.** {palier(a['score'], cfg.score)} **{a['score']}**/100 · {euros(a['marge'], signe=True)} · "
            f"ROI {pourcentage(a['roi'])}\n{_lien(a['titre'], a['url'])}\n"
            f"*{echapper_markdown(a['veille'], 40)} · {euros(a['prix'])} · {quand}*"
        )
    embed["description"] = "\n\n".join(morceaux)
    embed["footer"] = {"text": "Achat toujours manuel : ouvrez le lien depuis Vinted."}
    return embed


# --------------------------------------------------------------------------- /veilles


def embed_veilles(cfg: Config, base: Base, maintenant: float) -> dict[str, Any]:
    passages = base.derniers_passages()
    resume = base.resume_annonces_par_veille(maintenant - JOUR)
    champs: list[dict[str, Any]] = []
    for v in cfg.veilles[:25]:
        derniers = [t for cle, t in passages.items() if cle == v.nom or cle.startswith(v.nom + "#")]
        dernier = temps_ecoule(max(derniers), maintenant) if derniers else "jamais exécutée"
        r = resume.get(v.nom)
        activite = (f"{r['nb']} alerte(s) en 24 h · meilleur score {r['score_max']} · "
                    f"meilleure marge {euros(r['marge_max'], signe=True)}") if r else "aucune alerte en 24 h"
        recherche = cfg.recherche(v)
        champs.append({
            "name": echapper_markdown(v.nom, 90),
            "value": (f"Toutes les {v.intervalle_secondes} s · marge ≥ {euros(v.marge_min)} · score ≥ {v.score_min}\n"
                      f"Recherche : {echapper_markdown(recherche.texte or '(sans texte)', 60)} ({recherche.domaine})\n"
                      f"Dernier passage : {dernier}\n{activite}")[:1024],
            "inline": False,
        })
    embed: dict[str, Any] = {"title": f"👀 Veilles surveillées ({len(cfg.veilles)})", "color": COULEUR_VEILLES,
                             "fields": champs}
    if not cfg.veilles:
        embed["description"] = "Aucune veille définie dans config.yaml."
    elif len(cfg.veilles) > 25:
        embed["footer"] = {"text": f"{len(cfg.veilles) - 25} veille(s) non affichée(s)."}
    return embed


# --------------------------------------------------------------------------- /etat


def lire_statut(base: Base) -> dict[str, Any] | None:
    brut = base.lire_meta("statut_bot")
    if not brut:
        return None
    try:
        statut = json.loads(brut)
    except ValueError:
        return None
    return statut if isinstance(statut, dict) else None


def embed_etat(cfg: Config, base: Base, maintenant: float) -> dict[str, Any]:
    statut = lire_statut(base)
    # Sans nouvelles depuis 3 intervalles (au moins 3 min), on suppose le moteur arrêté.
    delai_max = max(180.0, 3 * max([v.intervalle_secondes or cfg.intervalle_secondes for v in cfg.veilles] or [60]))
    if statut is None:
        return {"title": "⚪ État du bot", "color": COULEUR_INACTIF,
                "description": "Aucun signal du moteur : il n'a jamais démarré avec cette base de données."}

    age = maintenant - float(statut.get("maj", 0))
    if age > delai_max:
        titre, couleur = "⚪ Bot silencieux", COULEUR_INACTIF
        description = (f"Aucun signal depuis {temps_ecoule(statut.get('maj'), maintenant).removeprefix('il y a ')} : "
                       "le moteur est probablement arrêté (voir les logs ou `systemctl status bot-vinted`).")
    elif statut.get("etat") == "pause":
        reprise = float(statut.get("pause_jusqu_a") or maintenant)
        heure = datetime.fromtimestamp(reprise).strftime("%H:%M:%S")
        restant = max(0, int(reprise - maintenant))
        titre, couleur = "🟠 Bot en pause", COULEUR_PAUSE
        description = (f"Reprise vers **{heure}** (dans {restant // 60} min {restant % 60:02d} s).\n"
                       f"Motif : {echapper_markdown(statut.get('motif') or 'inconnu', 200)}\n"
                       f"Échecs consécutifs : {statut.get('echecs', 0)}. Le bot ralentit seul et ne contourne rien.")
    else:
        titre, couleur = "🟢 Bot actif", COULEUR_ACTIF
        description = f"Dernier signal : {temps_ecoule(statut.get('maj'), maintenant)}."
    if statut.get("dry_run"):
        description += "\n⚠️ Mode simulation (--dry-run) : rien n'est envoyé."

    resume = base.resume_annonces_par_veille(maintenant - JOUR)
    total = sum(int(r["nb"]) for r in resume.values())
    return {"title": titre, "description": description, "color": couleur,
            "fields": [{"name": "Alertes sur 24 h", "value": str(total), "inline": True},
                       {"name": "Veilles", "value": str(len(cfg.veilles)), "inline": True}]}


# --------------------------------------------------------------------------- /stats


def embed_stats(base: Base) -> dict[str, Any]:
    s = calculer_stats(base.tous_les_achats())
    if s.achats == 0:
        return {"title": "📊 Résultats réels", "color": COULEUR_STATS,
                "description": "Aucun achat enregistré. Utilisez le bouton **J'ai acheté** d'une alerte, ou `/acheter`."}

    def eur(x: float | None, signe: bool = False) -> str:
        return "—" if x is None else euros(x, signe=signe)

    def pct(x: float | None) -> str:
        return "—" if x is None else pourcentage(x)

    def jours(x: float | None) -> str:
        return "—" if x is None else f"{x:.1f} j"

    champs: list[dict[str, Any]] = [
        {"name": "Achats", "value": f"{s.achats} ({s.vendus} revendu(s), {s.en_stock} en stock)", "inline": True},
        {"name": "Capital immobilisé", "value": eur(s.capital_immobilise), "inline": True},
        {"name": "Marge réelle totale", "value": eur(s.marge_totale, signe=True), "inline": True},
        {"name": "Marge moyenne / article", "value": eur(s.marge_moyenne, signe=True), "inline": True},
        {"name": "ROI moyen", "value": f"{pct(s.roi_moyen)} (global {pct(s.roi_global)})", "inline": True},
        {"name": "Délai moyen de revente", "value": jours(s.delai_moyen_jours), "inline": True},
    ]
    if s.nb_comparables:
        ecart = s.marge_reelle_moyenne_comparable - s.marge_estimee_moyenne
        champs.append({
            "name": f"Estimation du bot vs réalité ({s.nb_comparables} article(s))",
            "value": (f"Estimée {eur(s.marge_estimee_moyenne, signe=True)} · réelle "
                      f"{eur(s.marge_reelle_moyenne_comparable, signe=True)} · écart {eur(ecart, signe=True)}\n"
                      f"{conseil_calibrage(s.marge_estimee_moyenne, ecart)}")[:1024],
            "inline": False,
        })
    classement = [
        f"**{i}.** {echapper_markdown(v.veille, 40)} : {eur(v.marge_totale, signe=True)} sur {v.vendus} vente(s) "
        f"(ROI {pct(v.roi_moyen)})" for i, v in enumerate(s.par_veille[:8], 1) if v.veille != SANS_VEILLE or v.vendus
    ]
    if classement:
        champs.append({"name": "Veilles les plus rentables", "value": "\n".join(classement)[:1024], "inline": False})
    return {"title": "📊 Résultats réels", "color": COULEUR_STATS, "fields": champs}


# --------------------------------------------------------------------------- achats / ventes


def enregistrer_achat(base: Base, cfg: Config, reference: str, prix: float, maintenant: float, *,
                      domaine: str | None = None) -> tuple[bool, str]:
    """(succès, message) : même logique que la commande `acheter` du terminal."""
    try:
        return True, acheter(base, reference, prix, maintenant, domaine_defaut=cfg.domaine, domaine=domaine)
    except (ErreurSuivi, ValueError) as e:
        return False, str(e)


def enregistrer_vente(base: Base, cfg: Config, reference: str, prix: float, maintenant: float, *,
                      domaine: str | None = None) -> tuple[bool, str]:
    """(succès, message) : même logique que la commande `vendu` du terminal."""
    try:
        return True, vendre(base, reference, prix, maintenant, domaine_defaut=cfg.domaine, domaine=domaine)
    except (ErreurSuivi, ValueError) as e:
        return False, str(e)
