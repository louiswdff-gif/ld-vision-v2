"""Base SQLite : dédoublonnage, historique des prix, annonces alertées, achats/ventes.

`sqlite3` (bibliothèque standard, mode WAL) : les requêtes locales prennent quelques millisecondes,
inutile d'ajouter une couche asynchrone. Les dates sont des timestamps Unix (secondes).

Tables :
- veilles_etat     : premier / dernier passage de chaque veille (détecte le « premier lancement »)
- annonces_vues    : chaque annonce déjà traitée → jamais deux alertes pour la même annonce
- prix_historique  : prix demandés vus par (marque, catégorie, taille, état) → médiane de revente
- annonces         : annonces retenues (valorisation figée au moment de l'alerte) + statut d'envoi
- achats           : suivi réel des achats / reventes (commandes `acheter`, `vendu`, `stats`)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

VERSION_SCHEMA = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    cle TEXT PRIMARY KEY,
    valeur TEXT
);

CREATE TABLE IF NOT EXISTS veilles_etat (
    veille TEXT PRIMARY KEY,
    premier_passage_le REAL NOT NULL,
    derniere_execution_le REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS annonces_vues (
    domaine TEXT NOT NULL,
    annonce_id INTEGER NOT NULL,
    veille TEXT NOT NULL,
    vu_le REAL NOT NULL,
    PRIMARY KEY (domaine, annonce_id)
);
CREATE INDEX IF NOT EXISTS idx_vues_date ON annonces_vues (vu_le);

CREATE TABLE IF NOT EXISTS prix_historique (
    domaine TEXT NOT NULL,
    annonce_id INTEGER NOT NULL,
    marque TEXT NOT NULL,
    categorie TEXT NOT NULL,
    taille TEXT NOT NULL,
    etat TEXT NOT NULL,
    prix REAL NOT NULL,
    vu_le REAL NOT NULL,
    PRIMARY KEY (domaine, annonce_id)
);
CREATE INDEX IF NOT EXISTS idx_prix_cle ON prix_historique (domaine, marque, categorie, taille, etat, vu_le);

CREATE TABLE IF NOT EXISTS annonces (
    domaine TEXT NOT NULL,
    annonce_id INTEGER NOT NULL,
    veille TEXT NOT NULL,
    titre TEXT NOT NULL,
    marque TEXT NOT NULL DEFAULT '',
    taille TEXT NOT NULL DEFAULT '',
    etat TEXT,
    url TEXT NOT NULL,
    photo_url TEXT,
    prix REAL NOT NULL,
    cout_reel REAL NOT NULL,
    revente_estimee REAL NOT NULL,
    source_revente TEXT NOT NULL,
    nb_references INTEGER NOT NULL DEFAULT 0,
    marge REAL NOT NULL,
    roi REAL NOT NULL,
    score INTEGER NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    publie_le REAL,
    vendeur_note REAL,
    vendeur_avis INTEGER,
    alerte_statut TEXT NOT NULL,      -- en_attente | envoyee | simulee (dry-run)
    alerte_le REAL,
    cree_le REAL NOT NULL,
    PRIMARY KEY (domaine, annonce_id)
);
CREATE INDEX IF NOT EXISTS idx_annonces_statut ON annonces (alerte_statut, cree_le);

CREATE TABLE IF NOT EXISTS achats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domaine TEXT NOT NULL,
    annonce_id INTEGER NOT NULL,
    veille TEXT,
    titre TEXT,
    prix_achat_total REAL NOT NULL,   -- total réellement payé, tout compris
    date_achat REAL NOT NULL,
    marge_estimee REAL,               -- marge estimée par le bot au moment de l'alerte (si connue)
    prix_vente_net REAL,              -- montant net reçu à la revente
    date_vente REAL,
    UNIQUE (domaine, annonce_id)
);
"""


class Base:
    def __init__(self, chemin: str | Path) -> None:
        if str(chemin) != ":memory:":
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        self._cx = sqlite3.connect(str(chemin))
        self._cx.row_factory = sqlite3.Row
        self._cx.execute("PRAGMA journal_mode=WAL")
        self._cx.execute("PRAGMA synchronous=NORMAL")
        self._cx.executescript(_SCHEMA)
        self._cx.execute("INSERT OR IGNORE INTO meta (cle, valeur) VALUES ('version_schema', ?)", (str(VERSION_SCHEMA),))
        self._cx.commit()

    def __enter__(self) -> "Base":
        return self

    def __exit__(self, *exc: object) -> None:
        self.fermer()

    def fermer(self) -> None:
        self._cx.close()

    # ------------------------------------------------------------------ helpers

    def _ecrire(self, sql: str, params: tuple | dict = ()) -> int:
        """Exécute une écriture dans sa propre transaction ; retourne le nombre de lignes modifiées."""
        with self._cx:
            return self._cx.execute(sql, params).rowcount

    def _lire(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._cx.execute(sql, params).fetchall()

    def _lire_un(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._cx.execute(sql, params).fetchone()

    # ------------------------------------------------------------------ veilles

    def etat_veille(self, veille: str) -> sqlite3.Row | None:
        """None tant que la veille n'a jamais été exécutée (= premier lancement)."""
        return self._lire_un("SELECT * FROM veilles_etat WHERE veille = ?", (veille,))

    def marquer_passage(self, veille: str, maintenant: float) -> None:
        """Enregistre un passage ; le premier fixe `premier_passage_le`."""
        self._ecrire(
            """INSERT INTO veilles_etat (veille, premier_passage_le, derniere_execution_le) VALUES (?, ?, ?)
               ON CONFLICT(veille) DO UPDATE SET derniere_execution_le = excluded.derniere_execution_le""",
            (veille, maintenant, maintenant),
        )

    # ------------------------------------------------------------------ dédoublonnage

    def est_vue(self, domaine: str, annonce_id: int) -> bool:
        return self._lire_un("SELECT 1 FROM annonces_vues WHERE domaine = ? AND annonce_id = ?",
                             (domaine, annonce_id)) is not None

    def marquer_vue(self, domaine: str, annonce_id: int, veille: str, maintenant: float) -> bool:
        """Marque l'annonce comme traitée. Retourne True si elle était NOUVELLE."""
        return self._ecrire(
            "INSERT OR IGNORE INTO annonces_vues (domaine, annonce_id, veille, vu_le) VALUES (?, ?, ?, ?)",
            (domaine, annonce_id, veille, maintenant),
        ) == 1

    # ------------------------------------------------------------------ historique des prix

    def enregistrer_prix(self, domaine: str, annonce_id: int, marque: str, categorie: str, taille: str, etat: str,
                         prix: float, maintenant: float) -> bool:
        """Ajoute un prix demandé à l'historique (une annonce n'y figure qu'une fois). True si ajouté."""
        return self._ecrire(
            """INSERT OR IGNORE INTO prix_historique
               (domaine, annonce_id, marque, categorie, taille, etat, prix, vu_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (domaine, annonce_id, marque, categorie, taille, etat, prix, maintenant),
        ) == 1

    def prix_references(self, domaine: str, marque: str, categorie: str, taille: str, etat: str, depuis: float,
                        exclure_id: int | None = None) -> list[float]:
        """Prix historiques de la combinaison depuis `depuis` (timestamp), hors annonce `exclure_id`."""
        lignes = self._lire(
            """SELECT prix FROM prix_historique
               WHERE domaine = ? AND marque = ? AND categorie = ? AND taille = ? AND etat = ?
                 AND vu_le >= ? AND annonce_id != ?""",
            (domaine, marque, categorie, taille, etat, depuis, exclure_id if exclure_id is not None else -1),
        )
        return [float(r["prix"]) for r in lignes]

    def purger(self, avant: float) -> tuple[int, int]:
        """Supprime l'historique de prix et les annonces vues plus anciens que `avant`. Retourne (prix, vues)."""
        return (
            self._ecrire("DELETE FROM prix_historique WHERE vu_le < ?", (avant,)),
            self._ecrire("DELETE FROM annonces_vues WHERE vu_le < ?", (avant,)),
        )

    # ------------------------------------------------------------------ annonces retenues / alertes

    def enregistrer_annonce(self, v: dict[str, Any]) -> bool:
        """Enregistre une annonce retenue. `v` : colonnes de la table `annonces`. False si déjà présente."""
        colonnes = (
            "domaine", "annonce_id", "veille", "titre", "marque", "taille", "etat", "url", "photo_url", "prix",
            "cout_reel", "revente_estimee", "source_revente", "nb_references", "marge", "roi", "score", "tags",
            "publie_le", "vendeur_note", "vendeur_avis", "alerte_statut", "alerte_le", "cree_le",
        )
        valeurs = {c: v.get(c) for c in colonnes}
        valeurs["marque"] = valeurs["marque"] or ""
        valeurs["taille"] = valeurs["taille"] or ""
        valeurs["tags"] = valeurs["tags"] or ""
        valeurs["nb_references"] = valeurs["nb_references"] or 0
        return self._ecrire(
            f"INSERT OR IGNORE INTO annonces ({', '.join(colonnes)}) VALUES ({', '.join(':' + c for c in colonnes)})",
            valeurs,
        ) == 1

    def annonce(self, domaine: str, annonce_id: int) -> sqlite3.Row | None:
        return self._lire_un("SELECT * FROM annonces WHERE domaine = ? AND annonce_id = ?", (domaine, annonce_id))

    def annonces_par_id(self, annonce_id: int) -> list[sqlite3.Row]:
        """Toutes les annonces portant cet identifiant (un même id peut exister sur plusieurs domaines)."""
        return self._lire("SELECT * FROM annonces WHERE annonce_id = ?", (annonce_id,))

    def alertes_en_attente(self, limite: int = 20) -> list[sqlite3.Row]:
        """Alertes à (re)envoyer, des plus anciennes aux plus récentes."""
        return self._lire(
            "SELECT * FROM annonces WHERE alerte_statut = 'en_attente' ORDER BY cree_le LIMIT ?", (limite,)
        )

    def marquer_alerte(self, domaine: str, annonce_id: int, statut: str, maintenant: float) -> None:
        self._ecrire(
            "UPDATE annonces SET alerte_statut = ?, alerte_le = ? WHERE domaine = ? AND annonce_id = ?",
            (statut, maintenant, domaine, annonce_id),
        )

    # ------------------------------------------------------------------ achats / reventes

    def achat(self, domaine: str, annonce_id: int) -> sqlite3.Row | None:
        return self._lire_un("SELECT * FROM achats WHERE domaine = ? AND annonce_id = ?", (domaine, annonce_id))

    def achats_par_id(self, annonce_id: int) -> list[sqlite3.Row]:
        return self._lire("SELECT * FROM achats WHERE annonce_id = ?", (annonce_id,))

    def enregistrer_achat(self, domaine: str, annonce_id: int, veille: str | None, titre: str | None,
                          prix_total: float, maintenant: float, marge_estimee: float | None) -> None:
        """Enregistre (ou corrige, si pas encore revendu) un achat. Lève ValueError si déjà vendu."""
        existant = self.achat(domaine, annonce_id)
        if existant is not None:
            if existant["date_vente"] is not None:
                raise ValueError(f"l'annonce {annonce_id} est déjà enregistrée comme vendue")
            self._ecrire("UPDATE achats SET prix_achat_total = ? WHERE domaine = ? AND annonce_id = ?",
                         (prix_total, domaine, annonce_id))
            return
        self._ecrire(
            """INSERT INTO achats (domaine, annonce_id, veille, titre, prix_achat_total, date_achat, marge_estimee)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (domaine, annonce_id, veille, titre, prix_total, maintenant, marge_estimee),
        )

    def enregistrer_vente(self, domaine: str, annonce_id: int, prix_net: float, maintenant: float) -> None:
        """Marque un achat comme revendu. Lève ValueError si l'achat est inconnu."""
        if self.achat(domaine, annonce_id) is None:
            raise ValueError(f"aucun achat enregistré pour l'annonce {annonce_id}")
        self._ecrire(
            "UPDATE achats SET prix_vente_net = ?, date_vente = ? WHERE domaine = ? AND annonce_id = ?",
            (prix_net, maintenant, domaine, annonce_id),
        )

    def tous_les_achats(self) -> list[sqlite3.Row]:
        return self._lire("SELECT * FROM achats ORDER BY date_achat")
