"""Chargement et validation de la configuration (config.yaml) et des secrets (.env).

Principes :
- toute valeur réglable vit dans `config.yaml` ; les valeurs par défaut ci-dessous ne sont là
  que pour qu'un fichier de config minimal fonctionne (config.exemple.yaml les documente toutes) ;
- une clé inconnue ou mal orthographiée dans le YAML est une ERREUR (pas ignorée en silence) ;
- les secrets (webhooks Discord, token Telegram) ne sont jamais dans config.yaml : on y met seulement
  le NOM de la variable d'environnement, la valeur vient de `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bot_vinted.vinted.constantes import ETATS_VALIDES, normaliser_domaine
from bot_vinted.vinted.url_recherche import RechercheEffective, UrlRechercheInvalide, analyser_url, recherche_de_la_veille

# Garde-fou volontairement NON configurable : jamais moins de 30 s entre deux passages d'une veille.
INTERVALLE_MIN_SECONDES = 30

Etat = Literal["neuf_avec_etiquette", "neuf_sans_etiquette", "tres_bon_etat", "bon_etat", "satisfaisant"]


class ErreurConfig(Exception):
    """Configuration invalide ou incomplète (message lisible, destiné à l'utilisateur)."""


class Modele(BaseModel):
    """Base de tous les modèles : refuse les clés inconnues (détecte les fautes de frappe)."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- achat-revente


class FraisAcheteur(Modele):
    """Frais de protection acheteur Vinted : fixe + pourcentage du prix."""

    fixe: float = Field(0.70, ge=0)
    pourcentage: float = Field(5.0, ge=0)


class Revente(Modele):
    """Estimation du prix de revente à partir de l'historique des prix vus."""

    decote: float = Field(0.85, gt=0, le=1)  # prix demandés ≠ prix vendus
    min_references: int = Field(10, ge=1)  # en dessous : prix_revente_cible de la veille
    # Si la combinaison exacte (marque + taille + état) a trop peu de références, élargir à « toutes tailles »
    # (même marque, même veille, même état) avant de se rabattre sur le prix cible.
    assouplir_taille: bool = True
    fenetre_jours: int = Field(60, ge=1)  # ancienneté max des références
    decote_lot: float = Field(0.90, gt=0, le=1)  # décote supplémentaire sur la valeur d'un lot
    quantite_max_lot: int = Field(30, ge=2)  # au-delà, le nombre détecté est jugé incohérent


class PoidsScore(Modele):
    ecart_mediane: float = Field(30, ge=0)
    marge: float = Field(25, ge=0)
    roi: float = Field(20, ge=0)
    etat: float = Field(10, ge=0)
    fraicheur: float = Field(10, ge=0)
    vendeur: float = Field(5, ge=0)


class BonusPepites(Modele):
    """Points ajoutés au score pour chaque signal « pépite » (total plafonné)."""

    marque_mal_orthographiee: float = Field(10, ge=0)
    marque_absente_du_champ: float = Field(6, ge=0)
    annonce_mal_valorisee: float = Field(8, ge=0)
    vente_pressee: float = Field(5, ge=0)
    lot: float = Field(3, ge=0)
    plafond: float = Field(20, ge=0)


class SeuilsPaliers(Modele):
    feu: int = Field(80, ge=0, le=100)  # 🔥 à partir de
    ok: int = Field(60, ge=0, le=100)  # ✅ à partir de

    @model_validator(mode="after")
    def _coherence(self) -> "SeuilsPaliers":
        if self.ok > self.feu:
            raise ValueError("seuils : `ok` ne peut pas dépasser `feu`")
        return self


class Score(Modele):
    poids: PoidsScore = PoidsScore()
    bonus: BonusPepites = BonusPepites()
    seuils: SeuilsPaliers = SeuilsPaliers()
    marge_pleine_eur: float = Field(40, gt=0)  # marge qui vaut 100/100 sur le critère « marge »
    roi_plein_pct: float = Field(100, gt=0)  # ROI qui vaut 100/100
    ecart_plein_pct: float = Field(50, gt=0)  # écart à la médiane qui vaut 100/100
    fraicheur_demi_vie_minutes: float = Field(10, gt=0)  # le critère fraîcheur perd la moitié toutes les X min
    vendeur_avis_confiance: int = Field(20, ge=1)  # nb d'avis à partir duquel la note vendeur est jugée fiable
    etats: dict[str, float] = {
        "neuf_avec_etiquette": 100,
        "neuf_sans_etiquette": 85,
        "tres_bon_etat": 70,
        "bon_etat": 45,
        "satisfaisant": 20,
    }

    @field_validator("etats")
    @classmethod
    def _etats_connus(cls, v: dict[str, float]) -> dict[str, float]:
        inconnus = set(v) - set(ETATS_VALIDES)
        if inconnus:
            raise ValueError(f"états inconnus dans score.etats : {sorted(inconnus)} (valides : {list(ETATS_VALIDES)})")
        return v


class Signaux(Modele):
    """Réglages de détection des annonces « pépites »."""

    mots_vente_pressee: list[str] = [
        "urgent", "urgente", "demenagement", "vide dressing", "videdressing", "a saisir", "cause depart",
    ]
    titre_vague_mots_max: int = Field(3, ge=1)  # titre de N mots ou moins = vague
    titres_generiques: list[str] = [
        "veste", "manteau", "pull", "sweat", "t-shirt", "tshirt", "jean", "pantalon", "chemise", "robe", "vetements",
        "vetement", "polo", "short", "chaussures", "baskets", "sac",
    ]
    photos_max: int = Field(2, ge=1)  # N photos ou moins = annonce peu mise en valeur


class Marques(Modele):
    fichier: str = "marques.yaml"  # variantes d'orthographe par marque (relatif à config.yaml)
    seuil_fuzzy: int = Field(86, ge=50, le=100)  # similarité mini (0-100) pour une faute de frappe
    longueur_min_mot: int = Field(6, ge=3)  # le fuzzy ne s'applique qu'aux marques d'au moins N lettres
    # Option `variantes_marque` d'une veille : nombre max de recherches supplémentaires (une par variante).
    variantes_max_par_veille: int = Field(3, ge=1, le=10)
    # Valeurs du champ « marque » équivalant à « pas de marque » (la marque est alors cherchée dans le titre).
    valeurs_champ_vide: list[str] = [
        "autre", "other", "sans marque", "no brand", "unbranded", "inconnu", "otro", "altro", "andere", "sin marca",
    ]


class ExclusionsGlobales(Modele):
    contrefacons: list[str] = ["type", "style", "inspired", "replica", "replique", "copie", "fake", "dupe"]
    defauts: list[str] = ["trou", "tache", "abime", "dechire"]


class DefautsVeille(Modele):
    """Valeurs appliquées aux veilles qui ne définissent pas leur propre seuil."""

    port_estime: float = Field(0.0, ge=0)
    marge_min: float = 20.0
    score_min: int = Field(60, ge=0, le=100)


# --------------------------------------------------------------------------- réseau / alertes / divers


class Backoff(Modele):
    initial_secondes: float = Field(60, ge=1)
    facteur: float = Field(2, ge=1)
    max_secondes: float = Field(1800, ge=1)
    alerte_apres: int = Field(2, ge=1)  # nb d'échecs consécutifs avant l'alerte « bot en pause »
    jitter_pct: float = Field(10, ge=0, le=50)


class Reseau(Modele):
    # En-tête User-Agent FIXE (valeur d'un navigateur courant). Ce n'est qu'un en-tête : aucune imitation
    # d'empreinte TLS/navigateur, aucune rotation, aucun proxy. Modifiable ici.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    )
    accept_language: str = "fr-FR,fr;q=0.9,en;q=0.5"
    timeout_secondes: float = Field(20, gt=0)
    delai_min_entre_requetes_secondes: float = Field(2.0, ge=0)
    cookie_duree_max_minutes: int = Field(60, ge=1)
    articles_par_page: int = Field(96, ge=1, le=200)  # taille d'une page de résultats de Vinted (détecte la dernière)
    # Détail de chaque annonce candidate (description, note du vendeur…). DÉSACTIVÉ : la liste de Vinted ne contient
    # ni description ni note de vendeur, et l'ancienne API de détail n'existe plus (lecture de la page d'annonce à venir).
    charger_details: bool = False
    max_details_par_cycle: int = Field(10, ge=0)  # appels « détail annonce » max par passage de veille
    backoff: Backoff = Backoff()


class Discord(Modele):
    webhook_defaut: str = "DISCORD_WEBHOOK_URL"  # NOM de la variable d'environnement (valeur dans .env)
    pseudo: str = "Veille Vinted"
    timeout_secondes: float = Field(10, gt=0)


class DiscordBot(Modele):
    """Bot Discord INTERACTIF (commandes /stats, /top…, boutons sur chaque alerte), en remplacement des webhooks.

    Il ne fait QUE détecter et suivre : le bouton d'achat est un simple lien vers l'annonce Vinted,
    jamais un achat automatique."""

    actif: bool = False
    token_env: str = "DISCORD_BOT_TOKEN"  # NOM de la variable d'environnement (valeur dans .env)
    serveur_id: int | None = None  # ID du serveur : commandes disponibles immédiatement (sinon jusqu'à 1 h)
    salon_id: int | None = None  # salon par défaut des alertes (obligatoire si actif)
    # IDs Discord autorisés à enregistrer achats/ventes (boutons et commandes). Vide = personne : lecture seule.
    utilisateurs_autorises: list[int] = []
    timeout_connexion_secondes: float = Field(30, gt=0)

    @model_validator(mode="after")
    def _salon_requis(self) -> "DiscordBot":
        if self.actif and self.salon_id is None:
            raise ValueError("`discord_bot.salon_id` est obligatoire quand le bot Discord est actif")
        return self


class Telegram(Modele):
    actif: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    timeout_secondes: float = Field(10, gt=0)


class Journal(Modele):
    fichier: str = "logs/bot_vinted.log"
    niveau: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    taille_max_mo: float = Field(5, gt=0)
    sauvegardes: int = Field(3, ge=0)


class BaseDonnees(Modele):
    chemin: str = "donnees/bot_vinted.db"
    # Historique de prix et annonces vues plus anciens sont purgés (jamais en dessous de revente.fenetre_jours).
    conservation_jours: int = Field(90, ge=1)


# --------------------------------------------------------------------------- veilles


class Veille(Modele):
    """Une recherche surveillée. Les champs explicites surchargent ceux extraits de `url_recherche`."""

    nom: str = Field(min_length=1)
    url_recherche: str | None = None
    domaine: str | None = None  # sinon déduit de l'URL de recherche, sinon domaine global

    # Filtres envoyés à Vinted (côté serveur)
    mots_cles: list[str] = []  # utilisés comme texte de recherche si l'URL n'en contient pas
    marques: list[int] = []  # IDs de marques Vinted
    categories: list[int] = []  # IDs de catégories Vinted
    tailles: list[int] = []  # IDs de tailles Vinted
    couleurs: list[int] = []
    matieres: list[int] = []

    # Filtres appliqués par le bot (côté client, APRÈS enregistrement dans l'historique de prix)
    etats: list[Etat] = []
    prix_min: float | None = Field(None, ge=0)
    prix_max: float | None = Field(None, ge=0)
    mots_exclus: list[str] = []
    exceptions_exclusion: list[str] = []  # mots de la liste globale autorisés pour cette veille
    vendeur_note_min: float | None = Field(None, ge=0, le=5)
    vendeur_avis_min: int | None = Field(None, ge=0)
    vendeur_pays: list[str] = []  # codes ISO (FR, BE…)
    photos_min: int | None = Field(None, ge=0)

    # Si la recherche contient une marque connue (marques.yaml), n'alerter que sur les annonces de CETTE marque.
    # La recherche de Vinted renvoie aussi d'autres marques, que le prix de revente cible de la veille ne concerne pas.
    marque_stricte: bool = True

    # Achat-revente
    port_estime: float | None = Field(None, ge=0)
    prix_revente_cible: float | None = Field(None, gt=0)
    marge_min: float | None = None
    score_min: int | None = Field(None, ge=0, le=100)

    # Exécution
    intervalle_secondes: int | None = None
    webhook: str | None = None  # NOM de variable d'environnement (webhook Discord de cette veille)
    salon_discord: int | None = None  # ID du salon de cette veille quand le bot Discord est actif
    variantes_marque: bool = False  # lance aussi des recherches avec les variantes orthographiques de la marque

    @field_validator("mots_cles", "mots_exclus", "exceptions_exclusion", mode="before")
    @classmethod
    def _mots_en_texte(cls, v):
        """YAML lit `501` ou `90` comme des nombres : on les convertit en mots (« levis, 501 »)."""
        if isinstance(v, list):
            return [str(m) if isinstance(m, (int, float)) and not isinstance(m, bool) else m for m in v]
        return v

    @field_validator("domaine")
    @classmethod
    def _domaine(cls, v: str | None) -> str | None:
        return normaliser_domaine(v) if v else v

    @field_validator("intervalle_secondes")
    @classmethod
    def _intervalle(cls, v: int | None) -> int | None:
        if v is not None and v < INTERVALLE_MIN_SECONDES:
            raise ValueError(
                f"intervalle de {v} s trop court : minimum {INTERVALLE_MIN_SECONDES} s (protection anti-blocage)"
            )
        return v

    @field_validator("vendeur_pays")
    @classmethod
    def _pays(cls, v: list[str]) -> list[str]:
        return [p.strip().upper() for p in v]

    @model_validator(mode="after")
    def _coherence(self) -> "Veille":
        if self.prix_min is not None and self.prix_max is not None and self.prix_min > self.prix_max:
            raise ValueError(f"veille « {self.nom} » : prix_min ({self.prix_min}) > prix_max ({self.prix_max})")
        if not (self.url_recherche or self.mots_cles or self.marques or self.categories):
            raise ValueError(
                f"veille « {self.nom} » : indiquez au moins `url_recherche`, `mots_cles`, `marques` ou `categories`"
            )
        if self.url_recherche:  # échec immédiat (au démarrage) plutôt qu'au premier passage
            try:
                analyser_url(self.url_recherche)
            except UrlRechercheInvalide as e:
                raise ValueError(f"veille « {self.nom} » : {e}") from e
        return self


# --------------------------------------------------------------------------- racine


class Config(Modele):
    domaine: str = "vinted.fr"
    intervalle_secondes: int = 60
    premier_lancement_minutes: int = Field(15, ge=1)  # au 1er passage d'une veille, ignorer les annonces plus vieilles
    pages_amorcage: int = Field(3, ge=1)  # pages lues au 1er passage pour amorcer l'historique de prix (sans alerter)

    frais_acheteur: FraisAcheteur = FraisAcheteur()
    revente: Revente = Revente()
    score: Score = Score()
    signaux: Signaux = Signaux()
    marques: Marques = Marques()
    exclusions_globales: ExclusionsGlobales = ExclusionsGlobales()
    defauts_veille: DefautsVeille = DefautsVeille()

    reseau: Reseau = Reseau()
    discord: Discord = Discord()
    discord_bot: DiscordBot = DiscordBot()
    telegram: Telegram = Telegram()
    journal: Journal = Journal()
    base: BaseDonnees = BaseDonnees()

    veilles: list[Veille] = []

    # Dossier du fichier de config : sert à résoudre les chemins relatifs (journal, base, marques).
    racine: Path = Field(default=Path("."), exclude=True)

    @field_validator("domaine")
    @classmethod
    def _domaine(cls, v: str) -> str:
        return normaliser_domaine(v)

    @field_validator("intervalle_secondes")
    @classmethod
    def _intervalle(cls, v: int) -> int:
        if v < INTERVALLE_MIN_SECONDES:
            raise ValueError(
                f"intervalle de {v} s trop court : minimum {INTERVALLE_MIN_SECONDES} s (protection anti-blocage)"
            )
        return v

    @model_validator(mode="after")
    def _completer_veilles(self) -> "Config":
        noms = [v.nom for v in self.veilles]
        doublons = sorted({n for n in noms if noms.count(n) > 1})
        if doublons:
            raise ValueError(f"noms de veilles en double : {doublons}")
        for v in self.veilles:
            # Les valeurs non précisées par la veille prennent les valeurs globales.
            if v.intervalle_secondes is None:
                v.intervalle_secondes = self.intervalle_secondes
            if v.port_estime is None:
                v.port_estime = self.defauts_veille.port_estime
            if v.marge_min is None:
                v.marge_min = self.defauts_veille.marge_min
            if v.score_min is None:
                v.score_min = self.defauts_veille.score_min
            if v.webhook is None:
                v.webhook = self.discord.webhook_defaut
        return self

    def recherche(self, veille: Veille) -> RechercheEffective:
        """Recherche finale d'une veille (URL + champs YAML explicites + domaine global par défaut)."""
        return recherche_de_la_veille(veille, self.domaine)

    # ----- chemins -----

    def chemin(self, relatif: str) -> Path:
        """Résout un chemin du YAML : absolu tel quel, relatif par rapport au dossier de config.yaml."""
        p = Path(relatif).expanduser()
        return p if p.is_absolute() else (self.racine / p)

    # ----- secrets -----

    @staticmethod
    def secret(nom_variable: str | None) -> str | None:
        """Valeur d'une variable d'environnement (chargée depuis .env), None si absente ou vide."""
        if not nom_variable:
            return None
        valeur = os.environ.get(nom_variable, "").strip()
        return valeur or None

    def variables_secretes(self) -> list[str]:
        """Noms des variables d'environnement dont dépend cette configuration."""
        noms: set[str] = set()
        if self.discord_bot.actif:  # le bot Discord remplace les webhooks : un seul secret, son token
            noms.add(self.discord_bot.token_env)
        else:
            noms.add(self.discord.webhook_defaut)
            noms.update(v.webhook for v in self.veilles if v.webhook)
        if self.telegram.actif:
            noms.update({self.telegram.token_env, self.telegram.chat_id_env})
        return sorted(noms)

    def secrets_manquants(self) -> list[str]:
        return [n for n in self.variables_secretes() if not self.secret(n)]

    def valeurs_secretes(self) -> list[str]:
        """Valeurs réelles des secrets (pour les masquer dans les journaux)."""
        return [v for n in self.variables_secretes() if (v := self.secret(n))]


def _formater_erreurs(e: ValidationError) -> str:
    lignes = []
    for err in e.errors():
        chemin = ".".join(str(p) for p in err["loc"]) or "(racine)"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = "clé inconnue (faute de frappe ?)"
        lignes.append(f"  - {chemin} : {msg}")
    return "\n".join(lignes)


def charger_config(chemin: str | Path = "config.yaml") -> Config:
    """Lit config.yaml (+ .env voisin), valide et retourne la configuration."""
    chemin = Path(chemin).expanduser()
    if not chemin.is_file():
        raise ErreurConfig(
            f"Fichier de configuration introuvable : {chemin}\n"
            "Copiez config.exemple.yaml en config.yaml puis adaptez-le."
        )
    # .env à côté de config.yaml, puis dans le dossier courant. Les vraies variables d'environnement l'emportent.
    load_dotenv(chemin.resolve().parent / ".env", override=False)
    load_dotenv(override=False)

    try:
        brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ErreurConfig(f"YAML invalide dans {chemin} :\n{e}") from e
    if not isinstance(brut, dict):
        raise ErreurConfig(f"{chemin} doit contenir un dictionnaire YAML (clé: valeur).")
    if "racine" in brut:
        raise ErreurConfig("La clé `racine` est réservée (elle est déduite de l'emplacement de config.yaml).")
    brut["racine"] = chemin.resolve().parent

    try:
        return Config.model_validate(brut)
    except ValidationError as e:
        raise ErreurConfig(f"Configuration invalide ({chemin}) :\n{_formater_erreurs(e)}") from e
