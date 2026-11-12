"""Détection de marques : variantes d'orthographe (marques.yaml) + fuzzy matching (rapidfuzz).

Sert à deux choses :
1. déterminer la marque EFFECTIVE d'une annonce (clé de l'historique de prix), même si le champ marque
   est vide ou mal renseigné ;
2. repérer les « pépites » : marque mal orthographiée, ou absente du champ mais présente dans le titre /
   la description (les acheteurs qui filtrent par marque ne voient pas ces annonces).

Règle de confiance : un champ « marque » renseigné (et non « Autre ») est AUTORITAIRE. On ne cherche une
marque dans le texte que si le champ est vide, afin d'éviter de prendre « type Carhartt » dans un titre
pour la marque de l'article.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from bot_vinted.config import ErreurConfig, Marques
from bot_vinted.texte import normaliser

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    marque: str  # nom canonique normalisé (ex. « ralph lauren »)
    nom_affiche: str  # nom canonique tel qu'écrit dans marques.yaml (ex. « ralph lauren »)
    trouve: str  # texte normalisé effectivement repéré (ex. « ralf lauren »)
    exacte: bool  # True si c'est l'écriture canonique, False pour une variante ou une faute de frappe
    methode: str  # « exacte » | « variante » | « fuzzy »
    score: float  # 100 pour exacte/variante, similarité (0-100) pour fuzzy


@dataclass(frozen=True)
class AnalyseMarque:
    marque: str  # marque effective (normalisée) servant de clé à l'historique de prix
    nom_affiche: str  # joli nom pour l'alerte
    signal: str | None  # None | « mal_orthographiee » | « absente_du_champ »
    trouve: str | None  # texte repéré à l'origine du signal
    source: str  # « champ » | « texte » | « aucune »
    risque_faux: bool = False  # marque souvent contrefaite (marques.yaml : `risque_faux: true`) → avertissement


@dataclass(frozen=True)
class _Marque:
    nom: str  # canonique, tel qu'écrit dans le fichier
    norm: str
    variantes: tuple[str, ...]  # normalisées, sans doublon, sans le nom canonique
    fuzzy: bool
    risque_faux: bool = False


class CatalogueMarques:
    def __init__(self, marques: dict[str, dict | list | None], reglages: Marques) -> None:
        self._reglages = reglages
        self._vides = {normaliser(v) for v in reglages.valeurs_champ_vide}
        self._par_norm: dict[str, _Marque] = {}
        for nom, definition in marques.items():
            variantes_brutes, fuzzy, risque_faux = _lire_definition(nom, definition)
            norm = normaliser(nom)
            if not norm:
                continue
            variantes: list[str] = []
            for v in variantes_brutes:
                vn = normaliser(v)
                if vn and vn != norm and vn not in variantes:
                    variantes.append(vn)
            self._par_norm[norm] = _Marque(str(nom), norm, tuple(variantes), fuzzy, risque_faux)

        self._variante_vers_marque: dict[str, str] = {}
        for m in self._par_norm.values():
            for v in m.variantes:
                self._variante_vers_marque.setdefault(v, m.norm)

        # Noms comparés en fuzzy : écriture canonique, variantes, et formes sans espace (« northface »).
        self._noms_fuzzy: list[str] = []
        self._fuzzy_vers_marque: dict[str, str] = {}
        for m in self._par_norm.values():
            if not m.fuzzy:
                continue
            for nom in (m.norm, *m.variantes):
                for forme in {nom, nom.replace(" ", "")}:
                    if len(forme.replace(" ", "")) >= reglages.longueur_min_mot and forme not in self._fuzzy_vers_marque:
                        self._fuzzy_vers_marque[forme] = m.norm
                        self._noms_fuzzy.append(forme)

    @classmethod
    def charger(cls, chemin: Path, reglages: Marques) -> "CatalogueMarques":
        if not chemin.is_file():
            raise ErreurConfig(f"Fichier de marques introuvable : {chemin} (réglage `marques.fichier` de config.yaml)")
        try:
            donnees = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ErreurConfig(f"YAML invalide dans {chemin} :\n{e}") from e
        if not isinstance(donnees, dict):
            raise ErreurConfig(f"{chemin} doit contenir « marque: [variantes] ».")
        return cls(donnees, reglages)

    def __len__(self) -> int:
        return len(self._par_norm)

    # ------------------------------------------------------------------ détection

    def detecter(self, texte: str | None) -> Detection | None:
        """Cherche une marque dans un texte : écriture exacte, puis variante listée, puis fuzzy (meilleur score)."""
        jetons = normaliser(texte).split()
        if not jetons:
            return None
        groupes = [" ".join(jetons[i:i + n]) for n in (3, 2, 1) for i in range(len(jetons) - n + 1)]

        for g in groupes:  # 1. écriture canonique
            if g in self._par_norm:
                m = self._par_norm[g]
                return Detection(m.norm, m.nom, g, True, "exacte", 100.0)
        for g in groupes:  # 2. variante listée dans marques.yaml
            if g in self._variante_vers_marque:
                m = self._par_norm[self._variante_vers_marque[g]]
                return Detection(m.norm, m.nom, g, False, "variante", 100.0)

        meilleur: tuple[float, str, str] | None = None  # 3. fuzzy : faute de frappe proche d'un nom connu
        for g in groupes:
            if len(g.replace(" ", "")) < self._reglages.longueur_min_mot - 1 or not self._noms_fuzzy:
                continue
            trouve = process.extractOne(g, self._noms_fuzzy, scorer=fuzz.ratio, score_cutoff=self._reglages.seuil_fuzzy)
            if trouve and (meilleur is None or trouve[1] > meilleur[0]):
                meilleur = (trouve[1], g, trouve[0])
        if meilleur:
            score, g, nom = meilleur
            m = self._par_norm[self._fuzzy_vers_marque[nom]]
            return Detection(m.norm, m.nom, g, False, "fuzzy", round(score, 1))
        return None

    def analyser(self, champ_marque: str | None, texte: str | None) -> AnalyseMarque:
        """Marque effective + signal éventuel, à partir du champ marque et du texte (titre, description)."""
        champ_norm = normaliser(champ_marque)
        if champ_norm and champ_norm not in self._vides:
            d = self.detecter(champ_marque)
            if d:  # marque du champ reconnue ; une faute dans le champ lui-même est aussi une pépite
                return AnalyseMarque(d.marque, d.nom_affiche, None if d.exacte else "mal_orthographiee", d.trouve, "champ",
                                     self._par_norm[d.marque].risque_faux)
            return AnalyseMarque(champ_norm, str(champ_marque).strip(), None, None, "champ")  # marque non répertoriée

        d = self.detecter(texte)
        if d:
            signal = "absente_du_champ" if d.exacte else "mal_orthographiee"
            return AnalyseMarque(d.marque, d.nom_affiche, signal, d.trouve, "texte", self._par_norm[d.marque].risque_faux)
        return AnalyseMarque(champ_norm or "inconnue", str(champ_marque or "").strip(), None, None, "aucune")

    def marque_dans_texte(self, texte: str | None) -> bool:
        """Vrai si le texte contient une marque connue (utilisé pour juger si un titre est « vague »)."""
        return self.detecter(texte) is not None

    def variantes_de(self, marque_norm: str) -> list[str]:
        """Variantes orthographiques listées pour une marque (normalisées)."""
        m = self._par_norm.get(marque_norm)
        return list(m.variantes) if m else []


def _lire_definition(nom: str, definition: dict | list | None) -> tuple[list[str], bool, bool]:
    """(variantes, fuzzy, risque_faux). Accepte `marque: [variantes]` ou la forme longue
    `marque: {variantes: [...], fuzzy: false, scanner: false, risque_faux: true}`.
    `scanner` ne concerne que la commande `marques-ids` ; `risque_faux` ajoute un avertissement aux alertes."""
    if definition is None:
        return [], True, False
    if isinstance(definition, list):
        return [str(v) for v in definition], True, False
    if isinstance(definition, dict):
        inconnues = set(definition) - {"variantes", "fuzzy", "scanner", "risque_faux"}
        if inconnues:
            raise ErreurConfig(f"marques : clés inconnues pour « {nom} » : {sorted(inconnues)}")
        return ([str(v) for v in definition.get("variantes") or []], bool(definition.get("fuzzy", True)),
                bool(definition.get("risque_faux", False)))
    raise ErreurConfig(f"marques : définition invalide pour « {nom} » (liste ou dictionnaire attendu)")
