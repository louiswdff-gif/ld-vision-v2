"""Moteur : planifie les veilles et exécute le pipeline d'un passage.

Garanties de conception :
- UN SEUL worker (une boucle séquentielle) : jamais deux requêtes Vinted en parallèle, quel que soit le
  nombre de veilles. Le client ajoute en plus un délai minimal entre deux requêtes ;
- en cas de 429 / 403 / défi anti-bot / panne : backoff exponentiel GLOBAL (même IP), UNE alerte
  « bot en pause » puis UNE alerte « reprise » ; jamais d'insistance, aucun contournement ;
- une annonce n'est marquée « vue » qu'une fois la décision prise : si un blocage survient en cours de route,
  elle sera réexaminée au prochain passage au lieu d'être perdue ;
- une alerte est enregistrée en base AVANT l'envoi (statut en_attente) : une panne Discord n'efface pas
  l'affaire, elle sera renvoyée plus tard.

Pipeline par annonce (voir README) :
  1. historique de prix  2. déjà vue ?  3. fraîcheur (1er passage)  4. filtres gratuits (catalogue)
  5. valorisation préliminaire (marge)  6. détail (description, photos, vendeur)  7. filtres du détail
  8. évaluation finale (signaux + score) → seuils marge ET score  9. enregistrement + alerte
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from bot_vinted.alertes.base import SEPARATEUR_TAGS, Alerte, Diffuseur
from bot_vinted.base import Base
from bot_vinted.config import Config, Veille
from bot_vinted.evaluation import Evaluation, evaluer
from bot_vinted.filtres import filtrer_apres_detail, filtrer_avant_detail, verifier_exclusions
from bot_vinted.marques import CatalogueMarques
from bot_vinted.score import palier
from bot_vinted.signaux import detecter_lot
from bot_vinted.texte import normaliser
from bot_vinted.valorisation import categorie_de, cle_historique
from bot_vinted.vinted.client import ClientVinted, ErreurReseau, ErreurVinted, VintedBloque
from bot_vinted.vinted.modeles import Annonce, completer, fusionner_vendeur
from bot_vinted.vinted.pause import EtatPause
from bot_vinted.vinted.url_recherche import RechercheEffective

log = logging.getLogger(__name__)

JOUR = 86400
DELAI_RENVOI_ALERTES = 60.0  # secondes avant de retenter l'envoi d'alertes en attente après un échec
JITTER_INTERVALLE = 0.10  # +0 à 10 % sur chaque intervalle, pour ne pas être strictement périodique
# Nombre de détails « introuvables » CONSÉCUTIFS à partir duquel on suspecte un problème systématique (endpoint
# modifié ou bloqué) plutôt que des annonces réellement vendues : pause + alerte au lieu d'écarter en silence.
SEUIL_DETAILS_INTROUVABLES = 5


@dataclass
class PlanRecherche:
    """Une recherche planifiée : la recherche principale d'une veille, ou l'une de ses variantes de marque."""

    veille: Veille
    recherche: RechercheEffective
    cle_etat: str  # clé en base du « premier passage » (nom de la veille, + variante le cas échéant)
    libelle: str  # pour les logs
    prochaine: float = 0.0  # instant (horloge murale) du prochain passage
    marque_attendue: str | None = None  # marque cherchée (normalisée) si elle figure dans marques.yaml


@dataclass
class Bilan:
    """Compteurs d'un passage (pour le journal)."""

    lues: int = 0
    nouvelles: int = 0
    alertes: int = 0
    details: int = 0
    rejets: Counter = field(default_factory=Counter)

    def resume(self) -> str:
        rejets = ", ".join(f"{n} {code}" for code, n in self.rejets.most_common()) or "aucun"
        return (f"{self.lues} lues, {self.nouvelles} nouvelles, {self.details} détail(s), "
                f"{self.alertes} alerte(s) ; écartées : {rejets}")


class Moteur:
    def __init__(
        self,
        cfg: Config,
        base: Base,
        client: ClientVinted,
        diffuseur: Diffuseur,
        marques: CatalogueMarques,
        *,
        dry_run: bool = False,
        horloge: Callable[[], float] = time.time,
        attendre: Callable[[float], Awaitable[bool]] | None = None,
        alea: Callable[[], float] = random.random,
        installer_signaux: bool = True,
    ) -> None:
        self.cfg, self.base, self.client, self.diffuseur, self.marques = cfg, base, client, diffuseur, marques
        self.dry_run = dry_run
        self.horloge = horloge
        self._alea = alea
        self._installer_signaux = installer_signaux
        self._stop = asyncio.Event()
        self._attendre = attendre or self._attendre_ou_arret
        self.pause = EtatPause(cfg.reseau.backoff, alea=alea)
        self._pause_jusqu_a = 0.0
        self._pause_depuis: float | None = None
        self._prochain_renvoi = 0.0
        self._dernier_motif: str | None = None  # motif du dernier échec (affiché par /etat sur Discord)
        self._details_introuvables = 0
        self._derniere_purge = 0.0
        self.plans: list[PlanRecherche] = []

    # ------------------------------------------------------------------ cycle de vie

    def demander_arret(self) -> None:
        """Arrêt propre (SIGINT / SIGTERM) : la requête en cours se termine, puis la boucle s'arrête."""
        if not self._stop.is_set():
            log.info("Arrêt demandé : fin du passage en cours puis sortie…")
        self._stop.set()

    async def _attendre_ou_arret(self, secondes: float) -> bool:
        """Attend `secondes` ; retourne True si l'arrêt a été demandé entre-temps."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secondes)
            return True
        except asyncio.TimeoutError:
            return False

    async def lancer(self, une_fois: bool = False) -> None:
        """Boucle principale. `une_fois` : un seul passage par recherche puis arrêt."""
        if self._installer_signaux:
            self._brancher_signaux()
        self.plans = self._construire_plans()
        self._purger_si_necessaire()
        mode = "DRY-RUN (rien n'est envoyé)" if self.dry_run else "alertes réelles"
        log.info("Démarrage : %d recherche(s) planifiée(s), mode %s.", len(self.plans), mode)
        self._publier_statut()

        try:
            while self.plans and not self._stop.is_set():
                await self._renvoyer_alertes_en_attente()
                plan = min(self.plans, key=lambda p: p.prochaine)
                attente = max(plan.prochaine, self._pause_jusqu_a) - self.horloge()
                if attente > 0 and await self._attendre(attente):
                    break
                await self._executer(plan)
                if une_fois:
                    self.plans.remove(plan)
                self._purger_si_necessaire()
        finally:
            log.info("Arrêt du bot.")

    def _brancher_signaux(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.demander_arret)
            except (NotImplementedError, RuntimeError):  # Windows, ou boucle non principale
                pass

    def _purger_si_necessaire(self) -> None:
        maintenant = self.horloge()
        if maintenant - self._derniere_purge < JOUR:
            return
        self._derniere_purge = maintenant
        jours = max(self.cfg.base.conservation_jours, self.cfg.revente.fenetre_jours)
        prix, vues = self.base.purger(maintenant - jours * JOUR)
        if prix or vues:
            log.info("Purge de la base (> %d jours) : %d prix, %d annonces vues supprimés.", jours, prix, vues)

    # ------------------------------------------------------------------ planification

    def _construire_plans(self) -> list[PlanRecherche]:
        plans: list[PlanRecherche] = []
        for veille in self.cfg.veilles:
            recherche = self.cfg.recherche(veille)
            for avert in recherche.avertissements:
                log.warning("Veille « %s » : %s", veille.nom, avert)
            attendue = self._marque_attendue(veille, recherche)
            plans.append(PlanRecherche(veille, recherche, veille.nom, veille.nom, marque_attendue=attendue))
            log.info("Veille « %s » : %s · toutes les %d s.", veille.nom, recherche.resume(), veille.intervalle_secondes)
            if attendue:
                log.info("  marque exigée : « %s » (désactivable avec `marque_stricte: false`).", attendue)
            if self._exige_vendeur(veille) and not self.cfg.reseau.charger_details:
                log.warning("Veille « %s » : filtres vendeur (note, avis, pays) IGNORÉS : ces informations ne figurent "
                            "pas dans la liste de Vinted (`reseau.charger_details` est désactivé). Le vendeur "
                            "est marqué « Inconnu » dans les alertes.", veille.nom)
            if veille.variantes_marque:
                for variante in self._recherches_variantes(veille, recherche):
                    cle = f"{veille.nom}#variante:{variante.texte}"
                    plans.append(PlanRecherche(veille, variante, cle, f"{veille.nom} [variante « {variante.texte} »]",
                                               marque_attendue=attendue))
                    log.info("  + recherche par variante de marque : « %s »", variante.texte)
        return plans

    def _marque_attendue(self, veille: Veille, recherche: RechercheEffective) -> str | None:
        """Marque (normalisée) à exiger : celle du texte de recherche, si elle est connue de marques.yaml."""
        if not veille.marque_stricte:
            return None
        d = self.marques.detecter(recherche.texte)
        return d.marque if d else None

    def _recherches_variantes(self, veille: Veille, recherche: RechercheEffective) -> list[RechercheEffective]:
        """Recherches supplémentaires où la marque du texte est remplacée par ses variantes orthographiques."""
        d = self.marques.detecter(recherche.texte)
        if d is None:
            log.warning("Veille « %s » : `variantes_marque` activé mais aucune marque connue dans « %s ».",
                        veille.nom, recherche.texte)
            return []
        jetons, cible = normaliser(recherche.texte).split(), d.trouve.split()
        pos = next((i for i in range(len(jetons) - len(cible) + 1) if jetons[i:i + len(cible)] == cible), None)
        if pos is None:
            return []
        variantes = [v for v in self.marques.variantes_de(d.marque) if v != d.trouve]
        return [
            recherche.variante(" ".join(jetons[:pos] + v.split() + jetons[pos + len(cible):]))
            for v in variantes[: self.cfg.marques.variantes_max_par_veille]
        ]

    # ------------------------------------------------------------------ exécution d'un passage

    async def _executer(self, plan: PlanRecherche) -> None:
        intervalle = plan.veille.intervalle_secondes or self.cfg.intervalle_secondes
        try:
            await self._passage(plan)
        except VintedBloque as e:
            await self._sur_echec(e.motif, e.attente)
        except ErreurReseau as e:
            await self._sur_echec(str(e), None)
        except ErreurVinted as e:
            # Réponse inattendue (404 persistant, format modifié…) : même prudence qu'un blocage. On ralentit et on
            # prévient au lieu de réinterroger Vinted à chaque intervalle.
            await self._sur_echec(f"réponse inattendue de Vinted ({e})", None)
        except Exception:  # un bug sur une annonce ne doit jamais arrêter la surveillance
            log.exception("Veille « %s » : erreur inattendue, le bot continue.", plan.libelle)
        else:
            if self.pause.succes():
                await self._annoncer_reprise()
        plan.prochaine = self.horloge() + intervalle * (1 + self._alea() * JITTER_INTERVALLE)
        self._publier_statut()

    def _publier_statut(self) -> None:
        """Écrit l'état du bot en base (« battement de cœur ») : lu par la commande /etat du bot Discord."""
        maintenant = self.horloge()
        en_pause = self.pause.en_difficulte and self._pause_jusqu_a > maintenant
        try:
            self.base.ecrire_meta("statut_bot", json.dumps({
                "maj": maintenant,
                "etat": "pause" if en_pause else "actif",
                "pause_jusqu_a": self._pause_jusqu_a if en_pause else None,
                "motif": self._dernier_motif if self.pause.en_difficulte else None,
                "echecs": self.pause.echecs,
                "dry_run": self.dry_run,
            }))
        except Exception:  # le statut est un confort : il ne doit jamais perturber la surveillance
            log.debug("Statut du bot non publié.", exc_info=True)

    async def _sur_echec(self, motif: str, attente_serveur: float | None) -> None:
        maintenant = self.horloge()
        self._dernier_motif = motif
        delai = self.pause.echec(attente_serveur)
        self._pause_jusqu_a = maintenant + delai
        if self._pause_depuis is None:
            self._pause_depuis = maintenant
        reprise = datetime.fromtimestamp(self._pause_jusqu_a).strftime("%H:%M:%S")
        log.warning("Vinted : %s. Pause de %.0f s (reprise vers %s), échec consécutif n°%d.",
                    motif, delai, reprise, self.pause.echecs)
        if self.pause.a_alerter():
            message = (f"⏸️ **Bot Vinted en pause** — {motif}.\n"
                       f"Le bot ralentit automatiquement (backoff exponentiel) et réessaiera vers {reprise}. "
                       f"Aucune action requise ; il n'insiste pas et ne contourne aucune protection.")
            if await self.diffuseur.envoyer_texte(message):
                self.pause.marquer_alerte()

    async def _annoncer_reprise(self) -> None:
        duree = self.horloge() - (self._pause_depuis or self.horloge())
        self._pause_depuis = None
        log.info("Vinted répond de nouveau : reprise normale après %.0f min.", duree / 60)
        await self.diffuseur.envoyer_texte(f"▶️ **Bot Vinted : reprise normale** après {max(1, round(duree / 60))} min de pause.")

    async def _passage(self, plan: PlanRecherche) -> None:
        recherche = plan.recherche
        premier = self.base.etat_veille(plan.cle_etat) is None
        pages = self.cfg.pages_amorcage if premier else 1
        bilan = Bilan()
        budget = [self.cfg.reseau.max_details_par_cycle]  # appels « détail » restants pour ce passage
        if premier:
            log.info("Veille « %s » : premier passage (amorçage de l'historique de prix sur %d page(s) ; l'existant "
                     "n'est pas alerté).", plan.libelle, pages)

        # On charge TOUTES les pages d'abord : l'historique de prix est alors complet avant la moindre évaluation
        # (au premier passage, les annonces fraîches de la page 1 profitent des références des pages suivantes).
        annonces: list[Annonce] = []
        ids_vus: set[int] = set()
        for page in range(1, pages + 1):
            lot = await self.client.catalogue(recherche, page=page)
            annonces += [a for a in lot if a.id not in ids_vus]  # une annonce peut glisser d'une page à l'autre
            ids_vus.update(a.id for a in lot)
            if len(lot) < self.cfg.reseau.articles_par_page:
                break  # dernière page
        bilan.lues = len(annonces)

        for annonce in annonces:
            self._historiser(plan, annonce)
        for annonce in annonces:  # ordre du catalogue : les plus récentes d'abord
            await self._traiter(plan, annonce, premier, bilan, budget)

        self.base.marquer_passage(plan.cle_etat, self.horloge())
        niveau = logging.INFO if (bilan.nouvelles or bilan.alertes or premier) else logging.DEBUG
        log.log(niveau, "Veille « %s » : %s.", plan.libelle, bilan.resume())

    # ------------------------------------------------------------------ pipeline d'une annonce

    def _historiser(self, plan: PlanRecherche, annonce: Annonce) -> None:
        """Étape 1 : enregistre le prix dans l'historique, pour TOUTES les annonces comparables, quels que soient
        les filtres de prix et d'état de la veille (sinon la médiane serait biaisée vers le bas).
        Sont écartés de l'historique : lots, contrefaçons, annonces avec défauts, mots exclus, invisibles."""
        veille, cfg = plan.veille, self.cfg
        if not annonce.visible or annonce.prix <= 0:
            return
        if detecter_lot(annonce.titre, cfg.revente.quantite_max_lot) is not None:
            return
        if verifier_exclusions(f"{annonce.titre} {annonce.marque}", "le titre", veille, cfg) is not None:
            return
        categorie = categorie_de(annonce, plan.recherche.categories, normaliser(veille.nom))
        marque = self.marques.analyser(annonce.marque, annonce.titre).marque
        cle = cle_historique(annonce, marque, categorie)
        self.base.enregistrer_prix(annonce.domaine, annonce.id, cle.marque, cle.categorie, cle.taille, cle.etat,
                                   annonce.prix, self.horloge())

    async def _traiter(self, plan: PlanRecherche, annonce: Annonce, premier: bool, bilan: Bilan,
                       budget: list[int]) -> None:
        veille, recherche = plan.veille, plan.recherche
        cfg, base = self.cfg, self.base
        maintenant = self.horloge()
        categorie = categorie_de(annonce, recherche.categories, normaliser(veille.nom))  # clé figée AVANT le détail (cohérence historique)

        # 2. Déjà vue : jamais deux alertes pour la même annonce.
        if base.est_vue(annonce.domaine, annonce.id):
            return
        bilan.nouvelles += 1

        def ecarter(code: str, detail: str = "") -> None:
            base.marquer_vue(annonce.domaine, annonce.id, plan.cle_etat, self.horloge())
            bilan.rejets[code] += 1
            log.debug("Annonce %s « %.60s » écartée — %s %s", annonce.id, annonce.titre, code, detail)

        # 3. La liste de Vinted ne donne PAS la date de publication. Premier passage d'une veille : l'âge est inconnu,
        #    donc tout l'existant est ignoré (aucune alerte sur l'existant). Ensuite, une annonce nouvelle est datée
        #    de sa première détection (au plus un intervalle après sa mise en ligne).
        if premier:
            age = annonce.age_minutes(maintenant)
            if age is None or age > cfg.premier_lancement_minutes:
                return ecarter("ancienne")
        elif annonce.publie_le is None:
            annonce = annonce.model_copy(update={"publie_le": maintenant})

        # 4. Filtres gratuits (données du catalogue).
        rejet = filtrer_avant_detail(annonce, veille, recherche, cfg)
        if rejet:
            return ecarter(rejet.code, rejet.detail)

        # 4b. Marque : la recherche de Vinted renvoie aussi d'autres marques, auxquelles le prix cible de la veille ne
        #     s'applique pas. Une marque absente du champ mais présente (même mal écrite) dans le titre est acceptée.
        if plan.marque_attendue:
            marque = self.marques.analyser(annonce.marque, annonce.titre).marque
            if marque != plan.marque_attendue:
                return ecarter("marque", f"« {marque} » au lieu de « {plan.marque_attendue} »")

        # 5. Valorisation préliminaire : inutile de charger le détail si la marge est déjà insuffisante.
        ev = evaluer(annonce, veille, categorie, cfg, base, self.marques, maintenant)
        if ev is None:
            return ecarter("non_estimable", "ni références suffisantes ni prix_revente_cible")
        if ev.valorisation.marge < (veille.marge_min or 0):
            return ecarter("marge", f"{ev.valorisation.marge:g} € < {veille.marge_min:g} €")

        # 6-7. Détail (description, vendeur) : seulement si `reseau.charger_details` est activé. Budget par passage :
        #      si épuisé, l'annonce n'est PAS marquée vue, elle sera réexaminée au passage suivant.
        if cfg.reseau.charger_details:
            annonce = await self._enrichir(plan, annonce, ecarter, budget, bilan)
            if annonce is None:  # écartée ou reportée
                return

        # 8. Évaluation finale (signaux + score) et seuils : marge ≥ X € ET score ≥ Y.
        ev = evaluer(annonce, veille, categorie, cfg, base, self.marques, self.horloge())
        if ev is None:
            return ecarter("non_estimable")
        if ev.valorisation.marge < (veille.marge_min or 0):
            return ecarter("marge", f"{ev.valorisation.marge:g} € < {veille.marge_min:g} €")
        if ev.score.total < (veille.score_min or 0):
            return ecarter("score", f"{ev.score.total} < {veille.score_min}")

        # 9. Enregistrement (avant l'envoi) puis alerte.
        await self._alerter(plan, annonce, ev)
        bilan.alertes += 1

    async def _enrichir(self, plan: PlanRecherche, annonce: Annonce, ecarter, budget: list[int],
                        bilan: Bilan) -> Annonce | None:
        """Charge le détail de l'annonce et applique les filtres qui en dépendent. None si elle est écartée/reportée."""
        veille, cfg = plan.veille, self.cfg
        if budget[0] <= 0:
            bilan.rejets["reportee"] += 1
            return None
        budget[0] -= 1
        bilan.details += 1
        try:
            detail = await self.client.detail(annonce.domaine, annonce.id)
        except (VintedBloque, ErreurReseau):
            raise  # blocage ou panne : géré par la pause globale (l'annonce sera réexaminée plus tard)
        except ErreurVinted as e:  # détail illisible : on écarte cette annonce sans interrompre tout le passage
            ecarter("detail_illisible", str(e))
            return None
        if detail is None:
            self._details_introuvables += 1
            if self._details_introuvables >= SEUIL_DETAILS_INTROUVABLES:
                self._details_introuvables = 0
                # Volontairement AVANT `ecarter` : l'annonce n'est pas marquée vue, elle sera réexaminée.
                raise ErreurVinted(f"{SEUIL_DETAILS_INTROUVABLES} détails d'annonces « introuvables » d'affilée "
                                   "(endpoint modifié ou bloqué ?)")
            ecarter("introuvable", "annonce supprimée ou vendue")
            return None
        self._details_introuvables = 0
        annonce = completer(annonce, detail)
        if (budget[0] > 0 and self._exige_vendeur(veille) and annonce.vendeur and annonce.vendeur.id
                and annonce.vendeur.note is None and annonce.vendeur.nb_avis is None):
            budget[0] -= 1
            profil = await self.client.vendeur(annonce.domaine, annonce.vendeur.id)
            if profil:
                annonce = annonce.model_copy(update={"vendeur": fusionner_vendeur(annonce.vendeur, profil)})
        rejet = filtrer_apres_detail(annonce, veille, cfg)
        if rejet:
            ecarter(rejet.code, rejet.detail)
            return None
        return annonce

    @staticmethod
    def _exige_vendeur(veille: Veille) -> bool:
        return (veille.vendeur_note_min is not None or veille.vendeur_avis_min is not None
                or bool(veille.vendeur_pays))

    # ------------------------------------------------------------------ alertes

    def _construire_alerte(self, annonce: Annonce, ev: Evaluation, veille: Veille) -> Alerte:
        marque = annonce.marque
        if not marque and ev.marque.source == "texte":
            marque = f"{ev.marque.nom_affiche} (détectée)"
        v = ev.valorisation
        return Alerte(
            domaine=annonce.domaine, annonce_id=annonce.id, veille=veille.nom, titre=annonce.titre, url=annonce.url,
            prix=annonce.prix, cout_reel=v.cout_reel, revente_estimee=v.revente_estimee,
            source_revente=v.source_revente, nb_references=v.nb_references, marge=v.marge, roi_pct=v.roi_pct,
            score=ev.score.total, palier=ev.score.palier, marque=marque, taille=annonce.taille, etat=annonce.etat,
            photo_url=annonce.photo_url, tags=list(ev.tags), publie_le=annonce.publie_le,
            vendeur_note=annonce.vendeur.note if annonce.vendeur else None,
            vendeur_avis=annonce.vendeur.nb_avis if annonce.vendeur else None, webhook=veille.webhook,
            salon_id=veille.salon_discord,
        )

    async def _alerter(self, plan: PlanRecherche, annonce: Annonce, ev: Evaluation) -> None:
        alerte = self._construire_alerte(annonce, ev, plan.veille)
        maintenant = self.horloge()
        self.base.enregistrer_annonce({
            "domaine": annonce.domaine, "annonce_id": annonce.id, "veille": plan.veille.nom, "titre": annonce.titre,
            "marque": alerte.marque, "taille": annonce.taille, "etat": annonce.etat, "url": annonce.url,
            "photo_url": annonce.photo_url, "prix": annonce.prix, "cout_reel": ev.valorisation.cout_reel,
            "revente_estimee": ev.valorisation.revente_estimee, "source_revente": ev.valorisation.source_revente,
            "nb_references": ev.valorisation.nb_references, "marge": ev.valorisation.marge,
            "roi": ev.valorisation.roi_pct, "score": ev.score.total, "tags": SEPARATEUR_TAGS.join(ev.tags),
            "publie_le": annonce.publie_le, "vendeur_note": alerte.vendeur_note, "vendeur_avis": alerte.vendeur_avis,
            "alerte_statut": "en_attente", "alerte_le": None, "cree_le": maintenant,
        })
        self.base.marquer_vue(annonce.domaine, annonce.id, plan.cle_etat, maintenant)
        log.info("%s %s | %s | marge %+.2f € | ROI %+.0f %% | score %d | %s", ev.score.palier, annonce.titre,
                 plan.veille.nom, ev.valorisation.marge, ev.valorisation.roi_pct, ev.score.total, annonce.url)
        if await self.diffuseur.envoyer_alerte(alerte, self.horloge()):
            self.base.marquer_alerte(annonce.domaine, annonce.id, "simulee" if self.dry_run else "envoyee",
                                     self.horloge())
        else:
            log.warning("Alerte %s non envoyée : conservée, nouvel essai dans %.0f s.", annonce.id, DELAI_RENVOI_ALERTES)
            self._prochain_renvoi = self.horloge() + DELAI_RENVOI_ALERTES

    async def _renvoyer_alertes_en_attente(self) -> None:
        """Renvoie les alertes dont l'envoi a échoué (panne Discord, limitation…)."""
        if self.dry_run or self.horloge() < self._prochain_renvoi:
            return
        en_attente = self.base.alertes_en_attente()
        if not en_attente:
            return
        log.info("Renvoi de %d alerte(s) en attente.", len(en_attente))
        veilles = {v.nom: v for v in self.cfg.veilles}
        for ligne in en_attente:
            veille = veilles.get(ligne["veille"])
            alerte = Alerte.depuis_ligne(ligne, palier(ligne["score"], self.cfg.score),
                                         veille.webhook if veille else self.cfg.discord.webhook_defaut,
                                         veille.salon_discord if veille else None)
            if not await self.diffuseur.envoyer_alerte(alerte, self.horloge()):
                self._prochain_renvoi = self.horloge() + DELAI_RENVOI_ALERTES
                return
            self.base.marquer_alerte(ligne["domaine"], ligne["annonce_id"], "envoyee", self.horloge())
