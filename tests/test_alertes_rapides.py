"""Alertes « chaque nouvelle annonce » : mode `toutes`, commandes Telegram et relecture à chaud par le moteur."""

from __future__ import annotations

import pytest

from bot_vinted.alertes.telegram import CanalTelegram
from bot_vinted.alertes_rapides import SaisieInvalide, analyser_saisie
from bot_vinted.base import Base
from bot_vinted.bot_telegram import CLE_DECALAGE, BotTelegram
from bot_vinted.config import Config, charger_config

from tests.test_moteur_e2e import Banc, Monde, config, veille

URL = "https://www.vinted.fr/catalog?search_text=carhartt&price_to=40"
CHAT = "4242"


# ------------------------------------------------------------------ analyse de la saisie


def test_saisie_lien_et_mots():
    assert analyser_saisie(f"voici {URL}.").url == URL
    assert analyser_saisie("  pull   ralph lauren ").mots == "pull ralph lauren"


@pytest.mark.parametrize("texte", ["", "   ", "https://example.com/catalog", "x" * 200])
def test_saisie_invalide(texte):
    with pytest.raises(SaisieInvalide):
        analyser_saisie(texte)


# ------------------------------------------------------------------ commandes


class CanalFactice:
    """Canal Telegram simulé : messages à recevoir en entrée, réponses enregistrées en sortie."""

    def __init__(self, messages=()):
        self.a_recevoir = list(messages)
        self.reponses: list[str] = []
        self.offsets: list = []

    async def recevoir(self, decalage, attente=25):
        self.offsets.append(decalage)
        lot, self.a_recevoir = self.a_recevoir, []
        return lot

    async def envoyer_texte(self, message):
        self.reponses.append(message)


def message(update_id: int, texte: str, chat: str = CHAT) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": int(chat)}, "text": texte}}


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:T")
    cfg = Config(discord={"actif": False}, telegram={"actif": True, "alertes_max": 2})
    base = Base(":memory:")
    canal = CanalFactice()
    yield BotTelegram(cfg, base, canal, horloge=lambda: 1000.0)
    base.fermer()


def test_ajouter_lister_pause_supprimer(bot):
    assert "Alerte 1 créée" in bot.repondre("/ajouter pull ralph lauren")
    assert "Alerte 2 créée" in bot.repondre(f"/ajouter {URL}")
    liste = bot.repondre("/liste")
    assert "1 · pull ralph lauren" in liste and URL in liste
    assert "en pause" in bot.repondre("/pause 1")
    assert "⏸️ 1" in bot.repondre("/liste")
    assert [r["actif"] for r in bot.base.alertes_rapides()] == [0, 1]
    assert "relancée" in bot.repondre("/reprendre 1")
    assert "supprimée" in bot.repondre("/supprimer 2")
    assert "introuvable" in bot.repondre("/supprimer 2")
    assert "numéro" in bot.repondre("/supprimer")


def test_lien_colle_seul_cree_une_alerte_et_limite(bot):
    assert "Alerte 1 créée" in bot.repondre(URL)
    bot.repondre("/ajouter manteau")
    assert "Limite atteinte" in bot.repondre("/ajouter jean levis")
    assert len(bot.base.alertes_rapides()) == 2


def test_messages_invalides_et_commande_inconnue(bot):
    assert "pas une recherche Vinted valide" in bot.repondre("/ajouter https://example.com/x")
    assert "Commande inconnue" in bot.repondre("/nimportequoi")
    assert "/ajouter" in bot.repondre("bonjour")
    assert "/ajouter" in bot.repondre("/start")
    assert "Aucune alerte" in bot.repondre("/liste@MonBot")  # suffixe @nom_du_bot ignoré
    assert bot.base.alertes_rapides() == []


async def test_seul_le_chat_autorise_est_ecoute_et_le_decalage_avance(bot):
    bot.canal.a_recevoir = [message(10, "/ajouter manteau"), message(11, "/ajouter intrus", chat="999")]
    assert await bot.traiter_une_fois() == 2
    assert [r["mots"] for r in bot.base.alertes_rapides()] == ["manteau"]
    assert len(bot.canal.reponses) == 1
    assert bot.base.lire_meta(CLE_DECALAGE) == "12"
    await bot.traiter_une_fois()
    assert bot.canal.offsets == [None, 12]


# ------------------------------------------------------------------ mise en forme et configuration


def test_texte_telegram_d_une_alerte_simple():
    from bot_vinted.alertes.base import PALIER_SIMPLE, SOURCE_SIMPLE, Alerte

    a = Alerte(domaine="vinted.fr", annonce_id=1, veille="Alerte 1", titre="Veste <Carhartt>", url="https://x/y",
               prix=25.0, cout_reel=0, revente_estimee=0, source_revente=SOURCE_SIMPLE, nb_references=0, marge=0,
               roi_pct=0, score=0, palier=PALIER_SIMPLE, marque="Carhartt", taille="L", publie_le=100.0)
    texte = CanalTelegram.construire_texte(a, 160.0)
    assert a.simple and "Veste &lt;Carhartt&gt;" in texte and "25,00 €" in texte and "il y a 1 min" in texte
    assert "Marge" not in texte and "Revente" not in texte


def test_intervalle_des_alertes_telegram_a_un_minimum(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("telegram: {actif: true, intervalle_alertes_secondes: 5}\nveilles:\n  - {nom: A, mots_cles: [x]}\n",
                 encoding="utf-8")
    with pytest.raises(Exception, match="trop court"):
        charger_config(f)


# ------------------------------------------------------------------ moteur


def veille_toutes(**kw):
    return veille(nom="Tout Carhartt", mode="toutes", prix_revente_cible=None, vendeur_note_min=None,
                  vendeur_avis_min=None, marque_stricte=False, **kw)


async def test_mode_toutes_signale_chaque_nouvelle_annonce_sans_calcul():
    monde = Monde()
    banc = Banc(monde, config(veille_toutes()))
    try:
        monde.catalogue = [monde.item(i, prix=70.0) for i in (1, 2, 3)]
        await banc.passage()
        assert banc.canal.alertes == []  # premier passage : l'existant n'est jamais signalé
        assert len(monde.recherches()) == 1  # une seule page lue, pas d'amorçage de l'historique

        monde.catalogue = [monde.item(9, "Veste Carhartt neuve", prix=30.0), monde.item(8, prix=39.0), *monde.catalogue]
        monde.t += 100
        await banc.passage()
        assert [a.annonce_id for a in banc.canal.alertes] == [9, 8]
        a = banc.canal.alertes[0]
        assert a.simple and a.palier == "🆕" and a.prix == 30.0 and a.marge == 0
        assert banc.base.alertes_en_attente() == []  # envoyées
        assert monde.chemins("/api/v2/items") == []  # aucun appel de détail
        monde.t += 100
        await banc.passage()
        assert len(banc.canal.alertes) == 2  # jamais deux fois la même
    finally:
        await banc.fermer()


async def test_alertes_telegram_relues_a_chaud_par_le_moteur():
    monde = Monde()
    cfg = config(telegram={"actif": True})
    cfg.veilles = []
    banc = Banc(monde, cfg)
    banc.moteur._alertes_dynamiques = True
    try:
        banc.moteur._synchroniser_alertes_rapides()
        assert banc.moteur.plans == []
        identifiant = banc.base.ajouter_alerte_rapide(url=URL, mots=None, maintenant=monde.t)
        banc.moteur._synchroniser_alertes_rapides()
        assert [p.libelle for p in banc.moteur.plans][0].startswith(f"Alerte {identifiant}")
        banc.moteur._synchroniser_alertes_rapides()  # idempotent
        assert len(banc.moteur.plans) == 1
        plan = banc.moteur.plans[0]
        assert plan.veille.mode == "toutes" and plan.veille.intervalle_secondes == 60 and plan.prochaine <= monde.t

        monde.catalogue = [monde.item(1, prix=20.0)]
        await banc.moteur._executer(plan)  # premier passage
        monde.catalogue = [monde.item(2, prix=15.0), *monde.catalogue]
        monde.t += 100
        await banc.moteur._executer(plan)
        assert [a.annonce_id for a in banc.canal.alertes] == [2]

        banc.base.activer_alerte_rapide(identifiant, False)
        banc.moteur._synchroniser_alertes_rapides()
        assert banc.moteur.plans == []
        banc.base.activer_alerte_rapide(identifiant, True)
        banc.base.supprimer_alerte_rapide(identifiant)
        banc.moteur._synchroniser_alertes_rapides()
        assert banc.moteur.plans == [] and banc.moteur._plans_dynamiques == {}
    finally:
        await banc.fermer()


async def test_boucle_sans_recherche_attend_les_alertes_telegram():
    monde = Monde()
    cfg = config(telegram={"actif": True})
    cfg.veilles = []
    banc = Banc(monde, cfg)
    banc.moteur._alertes_dynamiques = True
    banc.arret_apres = 3
    try:
        await banc.moteur.lancer()
        assert len(banc.attentes) == 3 and all(a == 5.0 for a in banc.attentes)  # guette /ajouter toutes les 5 s
    finally:
        await banc.fermer()
