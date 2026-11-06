from __future__ import annotations

import json

import httpx
import pytest

from bot_vinted.alertes import Alerte, Diffuseur, ErreurEnvoi, construire_diffuseur
from bot_vinted.alertes.base import euros, libelle_vendeur, temps_ecoule
from bot_vinted.alertes.console import CanalConsole
from bot_vinted.alertes.discord import CanalDiscord
from bot_vinted.alertes.telegram import CanalTelegram
from bot_vinted.config import Config

MAINTENANT = 1_731_000_000.0
WEBHOOK_DEFAUT = "https://discord.com/api/webhooks/111/SECRET_TOKEN_DEFAUT"
WEBHOOK_CARHARTT = "https://discord.com/api/webhooks/222/SECRET_TOKEN_CARHARTT"


def alerte(**kw) -> Alerte:
    d = dict(
        domaine="vinted.fr", annonce_id=4001, veille="Carhartt vestes", titre="Veste Carhartt Detroit",
        url="https://www.vinted.fr/items/4001-veste-carhartt-detroit", prix=28.0, cout_reel=36.6,
        revente_estimee=70.0, source_revente="cible", nb_references=0, marge=33.4, roi_pct=91.3, score=87, palier="🔥",
        marque="Carhartt", taille="L", etat="tres_bon_etat", photo_url="https://images1.vinted.net/4001.jpeg",
        tags=["🔤 Marque mal orthographiée (« carhart » → carhartt)", "⏱️ Vente pressée (« urgent »)"],
        publie_le=MAINTENANT - 180, vendeur_note=4.8, vendeur_avis=142, webhook="DISCORD_WEBHOOK_CARHARTT",
    )
    d.update(kw)
    return Alerte(**d)


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_DEFAUT)
    monkeypatch.setenv("DISCORD_WEBHOOK_CARHARTT", WEBHOOK_CARHARTT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TOKEN_TELEGRAM_SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    return Config(telegram={"actif": True})


class Dormeur:
    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def __call__(self, s: float) -> None:
        self.pauses.append(s)


def champs(embed: dict) -> dict[str, str]:
    return {c["name"]: c["value"] for c in embed["fields"]}


# ------------------------------------------------------------------ formats communs


def test_euros():
    assert euros(28) == "28,00 €" and euros(1234.5) == "1 234,50 €"
    assert euros(33.4, signe=True) == "+33,40 €" and euros(-5, signe=True) == "-5,00 €" and euros(-5) == "-5,00 €"


def test_temps_ecoule():
    assert temps_ecoule(None, MAINTENANT) == "date inconnue"
    assert temps_ecoule(MAINTENANT - 20, MAINTENANT) == "à l'instant"
    assert temps_ecoule(MAINTENANT - 180, MAINTENANT) == "il y a 3 min"
    assert temps_ecoule(MAINTENANT - 7500, MAINTENANT) == "il y a 2 h 05"
    assert temps_ecoule(MAINTENANT - 3 * 86400, MAINTENANT) == "il y a 3 j"
    assert temps_ecoule(MAINTENANT + 500, MAINTENANT) == "à l'instant"  # horloge décalée : jamais négatif


def test_libelle_vendeur():
    assert libelle_vendeur(4.8, 142) == "4,8 ★ (142 avis)"
    assert libelle_vendeur(None, 0) == "Aucun avis"
    assert libelle_vendeur(None, None) == "Inconnu"
    assert libelle_vendeur(4.5, None) == "4,5 ★"


# ------------------------------------------------------------------ embed Discord


def test_embed_contient_tout_le_cahier_des_charges():
    e = CanalDiscord.construire_embed(alerte(), MAINTENANT)
    c = champs(e)
    assert e["title"] == "🔥 Veste Carhartt Detroit"
    assert e["url"].endswith("4001-veste-carhartt-detroit")
    assert e["image"]["url"] == "https://images1.vinted.net/4001.jpeg"  # photo principale
    assert c["Marque"] == "Carhartt" and c["Taille"] == "L" and c["État"] == "Très bon état"
    assert c["Prix affiché"] == "28,00 €"
    assert c["Coût réel"].startswith("36,60 €")
    assert c["Revente estimée"] == "70,00 € (prix cible de la veille)"
    assert c["Marge nette"] == "+33,40 €" and c["ROI"] == "+91 %" and c["Deal score"] == "🔥 87/100"
    assert "Marque mal orthographiée" in c["Signaux"] and "Vente pressée" in c["Signaux"]
    assert c["Vendeur"] == "4,8 ★ (142 avis)"
    assert c["Publiée"] == "il y a 3 min"
    assert "annonce 4001" in e["footer"]["text"] and "Carhartt vestes" in e["footer"]["text"]
    assert "(https://www.vinted.fr/items/4001" in e["description"]  # lien direct cliquable


def test_embed_revente_par_mediane():
    c = champs(CanalDiscord.construire_embed(alerte(source_revente="mediane", nb_references=14, revente_estimee=61.6),
                                             MAINTENANT))
    assert c["Revente estimée"] == "61,60 € (médiane de 14 annonces)"


@pytest.mark.parametrize("palier, couleur", [("🔥", 0xFF5722), ("✅", 0x2ECC71), ("🔹", 0x95A5A6)])
def test_couleur_selon_le_palier(palier, couleur):
    assert CanalDiscord.construire_embed(alerte(palier=palier), MAINTENANT)["color"] == couleur


def test_embed_sans_photo_sans_tags_sans_date():
    e = CanalDiscord.construire_embed(alerte(photo_url=None, tags=[], publie_le=None, vendeur_avis=None,
                                             vendeur_note=None, marque="", etat=None), MAINTENANT)
    assert "image" not in e and "timestamp" not in e
    c = champs(e)
    assert "Signaux" not in c and c["Publiée"] == "date inconnue" and c["Vendeur"] == "Inconnu"
    assert c["Marque"] == "—" and c["État"] == "Non précisé"


def test_embed_respecte_les_limites_discord():
    e = CanalDiscord.construire_embed(alerte(titre="T" * 400, tags=["x" * 500] * 5), MAINTENANT)
    assert len(e["title"]) <= 256
    assert all(len(c["value"]) <= 1024 and len(c["name"]) <= 256 for c in e["fields"])
    assert len(e["fields"]) <= 25


# ------------------------------------------------------------------ envoi Discord


async def test_envoi_discord_webhook_de_la_veille(cfg):
    recu: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        recu.append(r)
        return httpx.Response(204)

    canal = CanalDiscord(cfg, transport=httpx.MockTransport(handler))
    await canal.envoyer_alerte(alerte(webhook="DISCORD_WEBHOOK_CARHARTT"), MAINTENANT)
    await canal.envoyer_alerte(alerte(webhook=None), MAINTENANT)  # veille sans webhook propre → webhook par défaut
    await canal.fermer()

    assert [str(r.url) for r in recu] == [WEBHOOK_CARHARTT, WEBHOOK_DEFAUT]  # un salon différent par veille
    corps = json.loads(recu[0].content)
    assert corps["username"] == "Veille Vinted" and len(corps["embeds"]) == 1  # un embed par annonce


async def test_discord_429_respecte_retry_after_puis_reussit(cfg):
    reponses = [httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(204)]
    dormeur = Dormeur()
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: reponses.pop(0)), dormir=dormeur)
    await canal.envoyer_alerte(alerte(), MAINTENANT)
    assert dormeur.pauses == [1.5] and reponses == []
    await canal.fermer()


async def test_discord_429_attente_plafonnee_et_abandon_apres_3_tentatives(cfg):
    dormeur = Dormeur()
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"retry_after": 600})),
                         dormir=dormeur)
    with pytest.raises(ErreurEnvoi, match="3 tentatives"):
        await canal.envoyer_alerte(alerte(), MAINTENANT)
    assert dormeur.pauses == [30.0, 30.0]  # plafonné à 30 s, pas d'attente après la dernière tentative
    await canal.fermer()


async def test_discord_erreur_serveur_puis_succes(cfg):
    reponses = [httpx.Response(503), httpx.Response(204)]
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: reponses.pop(0)), dormir=Dormeur())
    await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.fermer()


@pytest.mark.parametrize("code", [400, 401, 404])
async def test_discord_webhook_invalide_pas_de_nouvelle_tentative(cfg, code):
    appels = []
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: (appels.append(1), httpx.Response(code))[1]),
                         dormir=Dormeur())
    with pytest.raises(ErreurEnvoi, match=f"HTTP {code}"):
        await canal.envoyer_alerte(alerte(), MAINTENANT)
    assert len(appels) == 1
    await canal.fermer()


async def test_discord_erreur_reseau(cfg):
    def coupure(r):
        raise httpx.ConnectError("boom", request=r)

    canal = CanalDiscord(cfg, transport=httpx.MockTransport(coupure), dormir=Dormeur())
    with pytest.raises(ErreurEnvoi, match="erreur réseau"):
        await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.fermer()


async def test_discord_variable_absente(cfg, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_CARHARTT")
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    with pytest.raises(ErreurEnvoi, match="DISCORD_WEBHOOK_CARHARTT"):
        await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.fermer()


async def test_le_webhook_n_apparait_jamais_dans_les_erreurs(cfg):
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(404)), dormir=Dormeur())
    with pytest.raises(ErreurEnvoi) as e:
        await canal.envoyer_alerte(alerte(), MAINTENANT)
    assert "SECRET_TOKEN" not in str(e.value)
    await canal.fermer()


async def test_discord_message_systeme(cfg):
    recu = []
    canal = CanalDiscord(cfg, transport=httpx.MockTransport(lambda r: (recu.append(r), httpx.Response(204))[1]))
    await canal.envoyer_texte("⏸️ Bot en pause")
    await canal.fermer()
    assert json.loads(recu[0].content)["content"] == "⏸️ Bot en pause"
    assert str(recu[0].url) == WEBHOOK_DEFAUT  # messages système → webhook par défaut


# ------------------------------------------------------------------ Telegram


def test_texte_telegram_meme_contenu_que_discord():
    t = CanalTelegram.construire_texte(alerte(), MAINTENANT)
    for attendu in ("Veste Carhartt Detroit", "Carhartt", "Très bon état", "28,00 €", "36,60 €", "70,00 €", "+33,40 €",
                    "+91 %", "87/100", "4,8 ★ (142 avis)", "il y a 3 min", "Marque mal orthographiée",
                    'href="https://www.vinted.fr/items/4001-veste-carhartt-detroit"'):
        assert attendu in t, attendu


def test_texte_telegram_echappe_le_html():
    t = CanalTelegram.construire_texte(alerte(titre="Veste <b>&</b> Co", tags=[]), MAINTENANT)
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in t and "<b>&</b>" not in t


def test_legende_telegram_limitee_a_1024_caracteres():
    t = CanalTelegram.construire_texte(alerte(tags=["x" * 300] * 6), MAINTENANT, limite=1024)
    assert len(t) <= 1024 and "Voir l'annonce" in t  # les signaux sautent avant le lien


async def test_telegram_envoie_photo_avec_bouton(cfg):
    recu: list[httpx.Request] = []
    canal = CanalTelegram(cfg, transport=httpx.MockTransport(lambda r: (recu.append(r), httpx.Response(200, json={"ok": True}))[1]))
    await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.fermer()
    assert recu[0].url.path.endswith("/sendPhoto")
    corps = json.loads(recu[0].content)
    assert corps["chat_id"] == "-100999" and corps["photo"].endswith("4001.jpeg") and corps["parse_mode"] == "HTML"
    assert corps["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("4001-veste-carhartt-detroit")


async def test_telegram_repli_sur_texte_si_photo_refusee(cfg):
    recu: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        recu.append(r.url.path.rsplit("/", 1)[1])
        if r.url.path.endswith("sendPhoto"):
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: wrong file identifier"})
        return httpx.Response(200, json={"ok": True})

    canal = CanalTelegram(cfg, transport=httpx.MockTransport(handler), dormir=Dormeur())
    await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.fermer()
    assert recu == ["sendPhoto", "sendMessage"]


async def test_telegram_sans_photo_envoie_un_message(cfg):
    recu: list[str] = []
    canal = CanalTelegram(cfg, transport=httpx.MockTransport(
        lambda r: (recu.append(r.url.path.rsplit("/", 1)[1]), httpx.Response(200, json={"ok": True}))[1]))
    await canal.envoyer_alerte(alerte(photo_url=None), MAINTENANT)
    await canal.fermer()
    assert recu == ["sendMessage"]


async def test_telegram_429_retry_after(cfg):
    reponses = [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}}),
                httpx.Response(200, json={"ok": True})]
    dormeur = Dormeur()
    canal = CanalTelegram(cfg, transport=httpx.MockTransport(lambda r: reponses.pop(0)), dormir=dormeur)
    await canal.envoyer_texte("pause")
    assert dormeur.pauses == [3.0]
    await canal.fermer()


async def test_telegram_token_jamais_dans_les_erreurs(cfg):
    canal = CanalTelegram(cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})), dormir=Dormeur())
    with pytest.raises(ErreurEnvoi) as e:
        await canal.envoyer_texte("x")
    assert "TOKEN_TELEGRAM_SECRET" not in str(e.value) and "Unauthorized" in str(e.value)
    await canal.fermer()


async def test_telegram_variables_absentes(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    canal = CanalTelegram(Config(telegram={"actif": True}))
    with pytest.raises(ErreurEnvoi, match="TELEGRAM_BOT_TOKEN"):
        await canal.envoyer_texte("x")
    await canal.fermer()


# ------------------------------------------------------------------ console (--dry-run) et diffusion


async def test_console_affiche_sans_rien_envoyer():
    sortie: list[str] = []
    canal = CanalConsole(sortie=sortie.append)
    await canal.envoyer_alerte(alerte(), MAINTENANT)
    await canal.envoyer_texte("pause")
    texte = sortie[0]
    for attendu in ("DRY-RUN", "Veste Carhartt Detroit", "+33,40 €", "87/100", "Marque mal orthographiée",
                    "il y a 3 min", "https://www.vinted.fr/items/4001"):
        assert attendu in texte, attendu
    assert "pause" in sortie[1]


def test_diffuseur_dry_run_ne_contient_que_la_console(cfg):
    d = construire_diffuseur(cfg, dry_run=True)
    assert [c.nom for c in d.canaux] == ["console"]


async def test_diffuseur_reel_discord_puis_telegram_si_actif(cfg):
    d = construire_diffuseur(cfg, dry_run=False)
    assert [c.nom for c in d.canaux] == ["discord", "telegram"]
    await d.fermer()
    d2 = construire_diffuseur(Config(), dry_run=False)
    assert [c.nom for c in d2.canaux] == ["discord"]
    await d2.fermer()


class CanalFactice:
    def __init__(self, nom: str, echoue: bool) -> None:
        self.nom, self.echoue, self.recus = nom, echoue, []

    async def envoyer_alerte(self, a, maintenant):
        if self.echoue:
            raise ErreurEnvoi("panne")
        self.recus.append(a)

    async def envoyer_texte(self, m):
        if self.echoue:
            raise ErreurEnvoi("panne")
        self.recus.append(m)

    async def fermer(self):
        return None


async def test_diffuseur_reussi_si_au_moins_un_canal_repond():
    principal, secours = CanalFactice("discord", False), CanalFactice("telegram", True)
    assert await Diffuseur([principal, secours]).envoyer_alerte(alerte(), MAINTENANT) is True
    assert len(principal.recus) == 1  # pas de doublon sur Discord si Telegram est en panne


async def test_diffuseur_echec_si_tous_les_canaux_echouent():
    d = Diffuseur([CanalFactice("discord", True), CanalFactice("telegram", True)])
    assert await d.envoyer_alerte(alerte(), MAINTENANT) is False
    assert await d.envoyer_texte("x") is False
