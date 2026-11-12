"""Couche discord.py : salons, boutons, formulaires et commandes, avec de FAUSSES interactions (aucune connexion)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")

from bot_vinted.alertes import Alerte, ErreurEnvoi, construire_diffuseur  # noqa: E402
from bot_vinted.base import Base  # noqa: E402
from bot_vinted.config import Config, ErreurConfig, Veille  # noqa: E402
from bot_vinted.discord_bot import bot as module_bot  # noqa: E402
from bot_vinted.discord_bot.bot import (  # noqa: E402
    BotDiscord,
    BoutonAchete,
    BoutonVendu,
    CanalBotDiscord,
    ModalPrix,
    construire_vue,
    url_sure,
)

T0 = 1_731_000_000.0
JOUR = 86400
AUTORISE, INTRUS = 42, 99


def reponse(statut: int) -> MagicMock:
    return MagicMock(status=statut, reason="test")


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot={"actif": True, "salon_id": 1000, "serveur_id": 555, "utilisateurs_autorises": [AUTORISE]},
        veilles=[Veille(nom="Carhartt vestes", mots_cles=["carhartt"], salon_discord=2000, prix_revente_cible=70)],
    )


class FauxSalon:
    def __init__(self) -> None:
        self.envois: list[dict] = []
        self.erreur: Exception | None = None

    async def send(self, **contenu) -> None:
        if self.erreur:
            raise self.erreur
        self.envois.append(contenu)


@pytest.fixture
async def bot(cfg, base):
    b = BotDiscord(cfg, base, horloge=lambda: T0)
    b.salons = {1000: FauxSalon(), 2000: FauxSalon()}
    b.is_ready = lambda: True
    b.get_channel = lambda ident: b.salons.get(ident)
    return b


def alerte(**kw) -> Alerte:
    d = dict(
        domaine="vinted.fr", annonce_id=4001, veille="Carhartt vestes", titre="Veste Carhartt Detroit",
        url="https://www.vinted.fr/items/4001-veste", prix=28.0, cout_reel=36.6, revente_estimee=70.0,
        source_revente="cible", nb_references=0, marge=33.4, roi_pct=91.3, score=87, palier="🔥", marque="Carhartt",
        taille="L", etat="tres_bon_etat", photo_url="https://images1.vinted.net/4001.jpeg", tags=["⏱️ Vente pressée"],
        publie_le=T0 - 180, vendeur_note=4.8, vendeur_avis=142, salon_id=2000,
    )
    d.update(kw)
    return Alerte(**d)


def annonce_en_base(base: Base, ident: int = 4001, **kw) -> None:
    v = dict(
        domaine="vinted.fr", annonce_id=ident, veille="Carhartt vestes", titre="Veste Carhartt Detroit",
        marque="Carhartt", taille="L", etat="tres_bon_etat", url=f"https://www.vinted.fr/items/{ident}-veste",
        photo_url=None, prix=28.0, cout_reel=36.6, revente_estimee=70.0, source_revente="cible", nb_references=0,
        marge=33.4, roi=91.3, score=87, tags="", publie_le=T0 - 180, vendeur_note=4.8, vendeur_avis=10,
        alerte_statut="envoyee", alerte_le=T0, cree_le=T0 - 120,
    )
    v.update(kw)
    base.enregistrer_annonce(v)


def interaction(bot, user_id: int = AUTORISE, avec_message: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        client=bot, user=SimpleNamespace(id=user_id), message=object() if avec_message else None,
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock(),
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def textes(i) -> str:
    """Contenu texte de la première réponse envoyée."""
    return i.response.send_message.call_args.args[0] if i.response.send_message.call_args.args else ""


# =============================================================================== vue et boutons


def test_url_sure():
    assert url_sure("https://www.vinted.fr/items/1-x", "vinted.fr", 1) == "https://www.vinted.fr/items/1-x"
    for mauvaise in (None, "", "javascript:alert(1)", "http://www.vinted.fr/items/1", "https://x y", "https://" + "a" * 600):
        assert url_sure(mauvaise, "vinted.be", 7) == "https://www.vinted.be/items/7"


async def test_vue_selon_l_avancement():
    a_acheter = construire_vue("https://www.vinted.fr/items/4001-veste", "vinted.fr", 4001)
    lien, bouton = a_acheter.children
    assert lien.style == discord.ButtonStyle.link and lien.url == "https://www.vinted.fr/items/4001-veste"
    assert isinstance(bouton, BoutonAchete) and bouton.item.custom_id == "vt:achete:vinted.fr:4001"
    assert a_acheter.timeout is None  # boutons persistants

    achete = construire_vue(None, "vinted.fr", 4001, "achete")
    assert achete.children[0].url == "https://www.vinted.fr/items/4001"  # lien reconstruit
    assert isinstance(achete.children[1], BoutonVendu) and achete.children[1].item.custom_id == "vt:vendu:vinted.fr:4001"

    vendu = construire_vue(None, "vinted.fr", 4001, "vendu")
    assert vendu.children[1].disabled is True and vendu.children[1].label == "Vendu"


async def test_aucun_bouton_d_achat_automatique():
    """Garde-fou de conception : les seuls boutons sont un lien, « J'ai acheté » (enregistrement) et « Vendu »."""
    for etat in ("a_acheter", "achete", "vendu"):
        vue = construire_vue("https://www.vinted.fr/items/1", "vinted.fr", 1, etat)
        for b in vue.children:
            libelle = (getattr(b, "label", None) or b.item.label).lower()
            assert "acheter" not in libelle.replace("j'ai acheté", "")  # pas de « Acheter maintenant »
        assert sum(1 for b in vue.children if getattr(b, "style", None) == discord.ButtonStyle.link) == 1


async def test_boutons_dynamiques_retrouvent_leurs_donnees_depuis_le_custom_id(bot):
    for classe, cid in ((BoutonAchete, "vt:achete:vinted.be:77"), (BoutonVendu, "vt:vendu:vinted.es:12")):
        m = classe.__discord_ui_compiled_template__.fullmatch(cid)
        bouton = await classe.from_custom_id(interaction(bot), MagicMock(), m)
        assert (bouton.domaine, bouton.annonce_id) == (m["domaine"], int(m["id"]))
        assert bouton.item.custom_id == cid
    # un identifiant forgé ne correspond pas au gabarit
    assert BoutonAchete.__discord_ui_compiled_template__.fullmatch("vt:achete:evil.com:1") is None


async def test_bouton_achete_ouvre_un_formulaire_prerempli_pour_un_utilisateur_autorise(bot, base):
    annonce_en_base(base)
    i = interaction(bot)
    await BoutonAchete("vinted.fr", 4001).callback(i)
    modal = i.response.send_modal.call_args.args[0]
    assert isinstance(modal, ModalPrix) and modal.mode == "achat"
    assert modal.prix.default == "36,60"  # coût réel estimé, modifiable
    i.response.send_message.assert_not_called()


async def test_bouton_achete_sans_annonce_en_base_n_a_pas_de_prix_par_defaut(bot):
    i = interaction(bot)
    await BoutonAchete("vinted.fr", 12345).callback(i)
    assert i.response.send_modal.call_args.args[0].prix.default is None


async def test_boutons_refuses_aux_utilisateurs_non_autorises(bot, base):
    annonce_en_base(base)
    for bouton in (BoutonAchete("vinted.fr", 4001), BoutonVendu("vinted.fr", 4001)):
        i = interaction(bot, user_id=INTRUS)
        await bouton.callback(i)
        i.response.send_modal.assert_not_called()
        assert "pas autorisé" in textes(i) and i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_liste_d_autorises_vide_equivaut_a_lecture_seule(base):
    b = BotDiscord(Config(discord_bot={"actif": True, "salon_id": 1}), base)
    i = interaction(b)
    await BoutonAchete("vinted.fr", 1).callback(i)
    i.response.send_modal.assert_not_called()


# =============================================================================== formulaires


async def test_formulaire_achat_enregistre_et_fait_evoluer_les_boutons(bot, base):
    annonce_en_base(base)
    modal = ModalPrix("achat", "vinted.fr", 4001, "36,60")
    modal.prix._value = "36,10"
    i = interaction(bot)
    await modal.on_submit(i)

    achat = base.achat("vinted.fr", 4001)
    assert achat["prix_achat_total"] == 36.10 and achat["veille"] == "Carhartt vestes" and achat["marge_estimee"] == 33.4
    vue = i.response.edit_message.call_args.kwargs["view"]
    assert isinstance(vue.children[1], BoutonVendu)  # « J'ai acheté » est devenu « Vendu »
    confirmation = i.followup.send.call_args.args[0]
    assert confirmation.startswith("✅") and "36.10 €" in confirmation and i.followup.send.call_args.kwargs["ephemeral"]


async def test_formulaire_vente_enregistre_marge_reelle_et_termine(bot, base):
    annonce_en_base(base)
    base.enregistrer_achat("vinted.fr", 4001, "Carhartt vestes", "Veste", 36.10, T0 - 5 * JOUR, 33.4)
    modal = ModalPrix("vente", "vinted.fr", 4001)
    modal.prix._value = "70"
    i = interaction(bot)
    await modal.on_submit(i)

    assert base.achat("vinted.fr", 4001)["prix_vente_net"] == 70.0
    vue = i.response.edit_message.call_args.kwargs["view"]
    assert vue.children[1].disabled is True  # « Vendu » terminé
    assert "+33.90 €" in i.followup.send.call_args.args[0] and "5.0 jour" in i.followup.send.call_args.args[0]


@pytest.mark.parametrize("saisie", ["", "abc", "-5", "1e999", "100001"])
async def test_formulaire_prix_invalide_ne_change_rien(bot, base, saisie):
    modal = ModalPrix("achat", "vinted.fr", 4001)
    modal.prix._value = saisie
    i = interaction(bot)
    await modal.on_submit(i)
    assert base.achat("vinted.fr", 4001) is None
    assert textes(i).startswith("❌") and i.response.send_message.call_args.kwargs["ephemeral"] is True
    i.response.edit_message.assert_not_called()


async def test_formulaire_revalide_l_autorisation_a_l_envoi(bot, base):
    modal = ModalPrix("achat", "vinted.fr", 4001)
    modal.prix._value = "30"
    i = interaction(bot, user_id=INTRUS)  # ex. droits retirés pendant la saisie, ou formulaire transmis
    await modal.on_submit(i)
    assert base.achat("vinted.fr", 4001) is None and "pas autorisé" in textes(i)


async def test_formulaire_erreur_metier_est_un_message_pas_une_exception(bot, base):
    modal = ModalPrix("vente", "vinted.fr", 999)  # vente d'un article jamais acheté
    modal.prix._value = "50"
    i = interaction(bot)
    await modal.on_submit(i)
    assert textes(i).startswith("❌") and "Aucun achat" in textes(i)
    i.response.edit_message.assert_not_called()  # le message d'alerte n'est pas modifié en cas d'échec


async def test_formulaire_sans_message_d_origine_repond_simplement(bot, base):
    annonce_en_base(base)
    modal = ModalPrix("achat", "vinted.fr", 4001)
    modal.prix._value = "30"
    i = interaction(bot, avec_message=False)
    await modal.on_submit(i)
    assert textes(i).startswith("✅") and i.response.edit_message.call_count == 0


# =============================================================================== commandes


def commande(bot, nom: str):
    return bot.tree.get_command(nom)


async def test_commandes_enregistrees_et_limitees_aux_serveurs(bot):
    noms = {c.name for c in bot.tree.get_commands()}
    assert noms == {"top", "veilles", "etat", "stats", "acheter", "vendu"}
    assert all(c.guild_only for c in bot.tree.get_commands())  # pas de commandes en message privé


async def test_commandes_de_lecture_ouvertes_a_tous_et_privees(bot, base):
    annonce_en_base(base)
    for nom, args in (("top", dict(heures=24, nombre=5)), ("veilles", {}), ("etat", {})):
        i = interaction(bot, user_id=INTRUS)  # même un membre non autorisé peut les lire
        await commande(bot, nom).callback(i, **args)
        envoi = i.response.send_message.call_args.kwargs
        assert isinstance(envoi["embed"], discord.Embed) and envoi["ephemeral"] is True
        assert envoi["allowed_mentions"].everyone is False  # jamais de mention
    i = interaction(bot)
    await commande(bot, "top").callback(i, heures=24, nombre=5)
    assert "Veste Carhartt Detroit" in i.response.send_message.call_args.kwargs["embed"].description


async def test_stats_reservee_aux_autorises(bot, base):
    i = interaction(bot, user_id=INTRUS)
    await commande(bot, "stats").callback(i)
    assert "pas autorisé" in textes(i) and "embed" not in i.response.send_message.call_args.kwargs
    i = interaction(bot)
    await commande(bot, "stats").callback(i)
    assert i.response.send_message.call_args.kwargs["embed"].title == "📊 Résultats réels"


async def test_commandes_acheter_puis_vendu(bot, base):
    annonce_en_base(base)
    i = interaction(bot)
    await commande(bot, "acheter").callback(i, annonce="https://www.vinted.fr/items/4001-veste", prix="36,10 €")
    assert textes(i).startswith("✅") and base.achat("vinted.fr", 4001)["prix_achat_total"] == 36.10
    i = interaction(bot)
    await commande(bot, "vendu").callback(i, annonce="4001", prix="70")
    assert textes(i).startswith("✅") and base.achat("vinted.fr", 4001)["prix_vente_net"] == 70.0


async def test_commandes_d_ecriture_refusees_et_erreurs_en_messages(bot, base):
    for nom in ("acheter", "vendu"):
        i = interaction(bot, user_id=INTRUS)
        await commande(bot, nom).callback(i, annonce="4001", prix="30")
        assert "pas autorisé" in textes(i)
    assert base.tous_les_achats() == []
    i = interaction(bot)
    await commande(bot, "acheter").callback(i, annonce="4001", prix="beaucoup")
    assert textes(i).startswith("❌") and "prix valide" in textes(i)
    i = interaction(bot)
    await commande(bot, "vendu").callback(i, annonce="999", prix="30")
    assert textes(i).startswith("❌") and "Aucun achat" in textes(i)


# =============================================================================== canal d'alertes


async def test_alerte_postee_dans_le_salon_de_la_veille_avec_ses_boutons(bot):
    await CanalBotDiscord(bot).envoyer_alerte(alerte(salon_id=2000), T0)
    assert bot.salons[1000].envois == []  # pas dans le salon par défaut
    envoi = bot.salons[2000].envois[0]
    embed = envoi["embed"]
    assert embed.title == "🔥 Veste Carhartt Detroit" and embed.url.endswith("4001-veste")
    assert {f.name for f in embed.fields} >= {"Marge nette", "ROI", "Deal score", "Coût réel", "Vendeur", "Signaux"}
    assert embed.image.url.endswith("4001.jpeg")
    lien, achete = envoi["view"].children
    assert lien.url == "https://www.vinted.fr/items/4001-veste" and isinstance(achete, BoutonAchete)
    assert envoi["allowed_mentions"].everyone is False and envoi["allowed_mentions"].users is False


async def test_alerte_sans_salon_de_veille_va_dans_le_salon_par_defaut(bot):
    await CanalBotDiscord(bot).envoyer_alerte(alerte(salon_id=None), T0)
    assert len(bot.salons[1000].envois) == 1 and bot.salons[2000].envois == []


async def test_titre_hostile_ne_peut_pas_pinger(bot):
    await CanalBotDiscord(bot).envoyer_alerte(alerte(titre="@everyone gratuit !!"), T0)
    envoi = bot.salons[2000].envois[0]
    assert envoi["allowed_mentions"].everyone is False  # discord n'enverra aucune notification de mention


async def test_envoi_impossible_tant_que_le_bot_n_est_pas_connecte(bot):
    bot.is_ready = lambda: False
    with pytest.raises(ErreurEnvoi, match="pas encore connecté"):
        await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)


async def test_salon_introuvable_puis_recupere_par_l_api(bot):
    bot.get_channel = lambda ident: None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(reponse(404), "inconnu"))
    with pytest.raises(ErreurEnvoi, match="salon 2000 introuvable"):
        await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)
    bot.fetch_channel = AsyncMock(return_value=bot.salons[2000])  # absent du cache mais récupérable
    await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)
    assert len(bot.salons[2000].envois) == 1


async def test_salon_non_textuel_refuse(bot):
    bot.get_channel = lambda ident: object()
    with pytest.raises(ErreurEnvoi, match="n'est pas un salon textuel"):
        await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)


async def test_erreurs_discord_a_l_envoi_deviennent_erreurs_d_envoi(bot):
    bot.salons[2000].erreur = discord.Forbidden(reponse(403), "Missing Permissions")
    with pytest.raises(ErreurEnvoi, match="permission refusée"):
        await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)
    bot.salons[2000].erreur = discord.HTTPException(reponse(500), "erreur")
    with pytest.raises(ErreurEnvoi, match="HTTP 500"):
        await CanalBotDiscord(bot).envoyer_alerte(alerte(), T0)


async def test_message_systeme_dans_le_salon_par_defaut(bot):
    await CanalBotDiscord(bot).envoyer_texte("⏸️ Bot en pause " + "x" * 3000)
    envoi = bot.salons[1000].envois[0]
    assert envoi["content"].startswith("⏸️") and len(envoi["content"]) == 2000


async def test_diffuseur_choisit_le_bot_puis_la_console_en_dry_run(bot, cfg):
    reel = construire_diffuseur(cfg, dry_run=False, bot=bot)
    assert [c.nom for c in reel.canaux] == ["discord-bot"]
    assert [c.nom for c in construire_diffuseur(cfg, dry_run=True, bot=bot).canaux] == ["console"]


# =============================================================================== connexion et synchronisation


async def test_demarrage_sans_token_est_refuse(bot, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(ErreurConfig, match="DISCORD_BOT_TOKEN"):
        await bot.demarrer()


async def test_token_refuse_par_discord(bot, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "jeton-bidon-123456")

    async def refuse(token, **kw):
        raise discord.LoginFailure("Improper token has been passed.")

    bot.start = refuse
    with pytest.raises(ErreurConfig, match="refusé le token"):
        await bot.demarrer()


async def test_demarrage_reussi_puis_arret_propre(bot, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "jeton-bidon-123456")
    recu = {}

    async def connecte(token, **kw):
        recu["token"] = token
        bot._pret.set()
        await asyncio.sleep(3600)  # connexion « vivante »

    bot.start = connecte
    await bot.demarrer()
    assert recu["token"] == "jeton-bidon-123456" and bot._pret.is_set()
    bot.close = AsyncMock()
    await bot.arreter()
    bot.close.assert_awaited_once()
    assert bot._tache.cancelled() or bot._tache.done()


async def test_connexion_lente_n_empeche_pas_le_demarrage(base, monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "jeton-bidon-123456")
    cfg = Config(discord_bot={"actif": True, "salon_id": 1, "timeout_connexion_secondes": 0.05})
    b = BotDiscord(cfg, base)

    async def jamais_pret(token, **kw):
        await asyncio.sleep(3600)

    b.start = jamais_pret
    with caplog.at_level("WARNING"):
        await b.demarrer()  # rend la main après le délai : les alertes seront conservées puis envoyées
    assert "pas encore connecté" in caplog.text
    b.close = AsyncMock()
    await b.arreter()


async def test_synchronisation_des_commandes_par_serveur_ou_globale(bot, cfg, base):
    bot.tree.sync = AsyncMock()
    bot.tree.copy_global_to = MagicMock()
    await bot._synchroniser_commandes()
    bot.tree.copy_global_to.assert_called_once()
    assert bot.tree.sync.call_args.kwargs["guild"].id == 555  # serveur configuré : disponible tout de suite

    b = BotDiscord(Config(discord_bot={"actif": True, "salon_id": 1}), base)
    b.tree.sync = AsyncMock()
    await b._synchroniser_commandes()
    b.tree.sync.assert_awaited_once_with()  # sans serveur : commandes globales


async def test_synchronisation_refusee_ne_plante_pas(bot, caplog):
    bot.tree.sync = AsyncMock(side_effect=discord.HTTPException(reponse(403), "Missing Access"))
    bot.tree.copy_global_to = MagicMock()
    with caplog.at_level("WARNING"):
        await bot._synchroniser_commandes()
    assert "applications.commands" in caplog.text


async def test_setup_hook_enregistre_les_boutons_persistants(bot):
    bot.add_dynamic_items = MagicMock()
    bot._synchroniser_commandes = AsyncMock()
    await bot.setup_hook()
    bot.add_dynamic_items.assert_called_once_with(BoutonAchete, BoutonVendu)
    bot._synchroniser_commandes.assert_awaited_once()


async def test_erreur_de_commande_repond_sans_fuite(bot):
    gestionnaire = bot.tree.on_error
    i = interaction(bot)
    await gestionnaire(i, discord.app_commands.AppCommandError("boom secret interne"))
    message = i.response.send_message.call_args.args[0]
    assert "boom secret interne" not in message and "logs" in message  # aucun détail technique côté Discord


def test_module_importable_et_constantes():
    assert module_bot.AUCUNE_MENTION.everyone is False


# =============================================================================== intégration avec le moteur


async def test_le_moteur_poste_via_le_bot_dans_le_salon_de_la_veille_puis_renvoie_si_echec():
    from bot_vinted.alertes.base import Diffuseur
    from tests.test_moteur_e2e import DEBUT, Banc, Monde, config, veille

    monde = Monde()
    cfg = config(veille(salon_discord=2000),
                 discord_bot={"actif": True, "salon_id": 1000, "utilisateurs_autorises": [AUTORISE]})
    ma_base = Base(":memory:")
    robot = BotDiscord(cfg, ma_base, horloge=monde)
    robot.salons = {1000: FauxSalon(), 2000: FauxSalon()}
    robot.is_ready = lambda: True
    robot.get_channel = lambda ident: robot.salons.get(ident)

    banc = Banc(monde, cfg, diffuseur=Diffuseur([CanalBotDiscord(robot)]))
    banc.moteur.base = banc.base = robot.base = ma_base  # le bot et le moteur partagent la même base
    banc.base.marquer_passage("Carhartt vestes", DEBUT - 1000)
    monde.catalogue = [monde.item(1, prix=25.0), monde.item(2, prix=24.0)]

    # 1) panne d'écriture dans le salon : les alertes sont conservées (rien de perdu)
    robot.salons[2000].erreur = discord.Forbidden(reponse(403), "Missing Permissions")
    await banc.passage()
    assert robot.salons[2000].envois == []
    assert {r["alerte_statut"] for r in banc.base.alertes_en_attente()} == {"en_attente"}
    assert len(banc.base.alertes_en_attente()) == 2

    # 2) permissions rétablies : le renvoi poste dans le salon DE LA VEILLE (pas dans le salon par défaut)
    robot.salons[2000].erreur = None
    monde.t += 120
    await banc.moteur._renvoyer_alertes_en_attente()
    assert robot.salons[1000].envois == [] and len(robot.salons[2000].envois) == 2
    assert banc.base.alertes_en_attente() == []
    premier = robot.salons[2000].envois[0]
    assert isinstance(premier["embed"], discord.Embed) and premier["view"].children[0].url.startswith("https://")

    # 3) clic « J'ai acheté » puis « Vendu » sur cette alerte : le suivi est alimenté depuis Discord
    modal = ModalPrix("achat", "vinted.fr", 1, "31,95")
    modal.prix._value = "31,95"
    clic = interaction(robot)
    await modal.on_submit(clic)
    achat = banc.base.achat("vinted.fr", 1)
    assert achat["prix_achat_total"] == 31.95 and achat["marge_estimee"] > 0
    await banc.fermer()
