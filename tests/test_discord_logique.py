from __future__ import annotations

import json
from datetime import datetime

import pytest

from bot_vinted.base import Base
from bot_vinted.config import Config, Veille
from bot_vinted.discord_bot.logique import (
    analyser_id_bouton,
    analyser_prix,
    autorise,
    echapper_markdown,
    embed_etat,
    embed_stats,
    embed_top,
    embed_veilles,
    enregistrer_achat,
    enregistrer_vente,
    id_bouton,
    lire_statut,
)

T0 = 1_731_000_000.0
JOUR = 86400


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot={"actif": True, "salon_id": 1, "utilisateurs_autorises": [42]},
        veilles=[
            Veille(nom="Carhartt vestes", url_recherche="https://www.vinted.fr/catalog?search_text=carhartt%20veste",
                   marge_min=20, score_min=60, intervalle_secondes=60),
            Veille(nom="Ralph Lauren", mots_cles=["ralph", "lauren"], marge_min=15, score_min=65, intervalle_secondes=120),
        ],
    )


def annonce(base: Base, ident: int, **kw) -> None:
    v = dict(
        domaine="vinted.fr", annonce_id=ident, veille="Carhartt vestes", titre=f"Veste {ident}", marque="Carhartt",
        taille="L", etat="tres_bon_etat", url=f"https://www.vinted.fr/items/{ident}-veste", photo_url=None, prix=28.0,
        cout_reel=36.6, revente_estimee=70.0, source_revente="cible", nb_references=0, marge=33.4, roi=91.3, score=80,
        tags="", publie_le=T0 - 180, vendeur_note=4.8, vendeur_avis=10, alerte_statut="envoyee", alerte_le=T0,
        cree_le=T0 - 120,
    )
    v.update(kw)
    base.enregistrer_annonce(v)


def champs(embed: dict) -> dict[str, str]:
    return {c["name"]: c["value"] for c in embed.get("fields", [])}


# ------------------------------------------------------------------ utilitaires


def test_echapper_markdown_neutralise_liens_formatage_et_mentions():
    hostile = "[Cliquez](http://evil.example) **gras** `code` @everyone <@123> :fire: > citation"
    sortie = echapper_markdown(hostile, longueur_max=200)
    for brut in ("[Cliquez]", "](http", "**gras**", "`code`", "<@123>", ":fire:"):
        assert brut not in sortie
    assert "@everyone" not in sortie and "@​everyone" in sortie  # plus de mention possible
    assert "\\[Cliquez\\]\\(http" in sortie  # le lien est devenu du texte inerte


def test_echapper_markdown_tronque_et_normalise_les_espaces():
    assert echapper_markdown("a  b\n\nc") == "a b c"
    assert len(echapper_markdown("x" * 500, longueur_max=50)) <= 51 and echapper_markdown("x" * 500, 50).endswith("…")


def test_autorise(cfg):
    assert autorise(cfg, 42) is True and autorise(cfg, 43) is False
    assert autorise(Config(), 42) is False  # liste vide : personne (lecture seule)


@pytest.mark.parametrize("texte, attendu", [("36,10", 36.10), ("36.10", 36.10), (" 36 € ", 36.0), ("1 234,5", 1234.5),
                                            ("0", 0.0), ("36,10 €", 36.10)])
def test_analyser_prix_valides(texte, attendu):
    assert analyser_prix(texte) == pytest.approx(attendu)


@pytest.mark.parametrize("texte", ["", "abc", "-5", "nan", "inf", "1e999", "100001", "12,3,4"])
def test_analyser_prix_invalides(texte):
    with pytest.raises(ValueError):
        analyser_prix(texte)


def test_identifiants_de_boutons_aller_retour():
    cid = id_bouton("achete", "vinted.fr", 4001)
    assert cid == "vt:achete:vinted.fr:4001" and len(cid) < 100
    assert analyser_id_bouton(cid) == ("achete", "vinted.fr", 4001)
    assert analyser_id_bouton(id_bouton("vendu", "vinted.co.uk", 7)) == ("vendu", "vinted.co.uk", 7)
    for invalide in ("", "vt:achete:vinted.fr:abc", "vt:inconnu:vinted.fr:1", "autre:achete:vinted.fr:1",
                     "vt:achete:evil.com:1", "vt:achete:vinted.fr:1:extra"):
        assert analyser_id_bouton(invalide) is None
    with pytest.raises(ValueError):
        id_bouton("supprimer", "vinted.fr", 1)


# ------------------------------------------------------------------ /top


def test_top_classe_par_score_puis_marge(base, cfg):
    annonce(base, 1, score=70, marge=50.0)
    annonce(base, 2, score=95, marge=10.0)
    annonce(base, 3, score=95, marge=30.0)
    e = embed_top(base, cfg, T0, heures=24, nombre=3)
    d = e["description"]
    assert d.index("Veste 3") < d.index("Veste 2") < d.index("Veste 1")
    assert "🔥" in d and "✅" in d and "(https://www.vinted.fr/items/3-veste)" in d
    assert "+30,00 €" in d and "il y a 3 min" in d
    assert "manuel" in e["footer"]["text"]


def test_top_respecte_periode_et_limites(base, cfg):
    annonce(base, 1, cree_le=T0 - 5 * 3600)
    assert "Veste 1" in embed_top(base, cfg, T0, heures=6)["description"]
    assert "Aucune annonce" in embed_top(base, cfg, T0, heures=2)["description"]
    assert "168" in embed_top(base, cfg, T0, heures=9999)["title"]  # borné à 7 jours
    for i in range(20):
        annonce(base, 100 + i)
    assert embed_top(base, cfg, T0, nombre=999)["description"].count("\n\n") == 9  # borné à 10 annonces


def test_top_titre_hostile_ne_forge_pas_de_lien(base, cfg):
    annonce(base, 1, titre="[Offre](http://evil.example) @everyone", url="https://www.vinted.fr/items/1-x")
    d = embed_top(base, cfg, T0)["description"]
    assert "](http://evil.example)" not in d and "@everyone" not in d
    assert "(https://www.vinted.fr/items/1-x)" in d  # le seul vrai lien


def test_top_refuse_les_urls_non_https(base, cfg):
    annonce(base, 1, url="javascript:alert(1)")
    annonce(base, 2, url="https://www.vinted.fr/items/2) [piège](http://evil.example")
    d = embed_top(base, cfg, T0)["description"]
    assert "javascript:" not in d and "http://evil.example)" not in d


# ------------------------------------------------------------------ /veilles


def test_veilles(base, cfg):
    annonce(base, 1, veille="Carhartt vestes", score=85, marge=40.0, cree_le=T0 - 3600)
    annonce(base, 2, veille="Carhartt vestes", score=70, marge=10.0, cree_le=T0 - 3600)
    base.marquer_passage("Carhartt vestes", T0 - 12)
    base.marquer_passage("Carhartt vestes#variante:carhart veste", T0 - 300)  # variante : on retient le plus récent
    e = embed_veilles(cfg, base, T0)
    assert "(2)" in e["title"]
    carhartt, ralph = e["fields"]
    assert carhartt["name"] == "Carhartt vestes"
    assert "Toutes les 60 s" in carhartt["value"] and "score ≥ 60" in carhartt["value"]
    assert "carhartt veste" in carhartt["value"] and "vinted.fr" in carhartt["value"]
    assert "Dernier passage : à l'instant" in carhartt["value"]  # 12 s ; la variante (300 s) est plus ancienne
    assert "2 alerte(s) en 24 h" in carhartt["value"]
    assert "meilleur score 85" in carhartt["value"] and "+40,00 €" in carhartt["value"]
    assert "jamais exécutée" in ralph["value"] and "aucune alerte en 24 h" in ralph["value"]


def test_veilles_vide_et_tronquee(base):
    assert "Aucune veille" in embed_veilles(Config(), base, T0)["description"]
    trop = Config(veilles=[Veille(nom=f"V{i}", mots_cles=["x"]) for i in range(30)])
    e = embed_veilles(trop, base, T0)
    assert len(e["fields"]) == 25 and "5 veille(s) non affichée(s)" in e["footer"]["text"]


# ------------------------------------------------------------------ /etat


def statut(base: Base, **kw) -> None:
    d = {"maj": T0 - 10, "etat": "actif", "pause_jusqu_a": None, "motif": None, "echecs": 0, "dry_run": False}
    d.update(kw)
    base.ecrire_meta("statut_bot", json.dumps(d))


def test_etat_actif(base, cfg):
    annonce(base, 1, cree_le=T0 - 100)
    statut(base)
    e = embed_etat(cfg, base, T0)
    assert e["title"] == "🟢 Bot actif" and "Dernier signal : à l'instant" in e["description"]
    assert e["color"] == 0x2ECC71
    assert champs(e)["Alertes sur 24 h"] == "1" and champs(e)["Veilles"] == "2"


def test_etat_en_pause_affiche_motif_et_reprise(base, cfg):
    statut(base, etat="pause", pause_jusqu_a=T0 + 125, motif="trop de requêtes (HTTP 429) **gras**", echecs=2)
    e = embed_etat(cfg, base, T0)
    assert e["title"] == "🟠 Bot en pause" and e["color"] == 0xE67E22
    assert datetime.fromtimestamp(T0 + 125).strftime("%H:%M:%S") in e["description"]
    assert "dans 2 min 05 s" in e["description"] and "HTTP 429" in e["description"]
    assert "**gras**" not in e["description"]  # motif échappé
    assert "Échecs consécutifs : 2" in e["description"]


def test_etat_silencieux_si_plus_de_nouvelles(base, cfg):
    statut(base, maj=T0 - 3600)
    e = embed_etat(cfg, base, T0)
    assert e["title"] == "⚪ Bot silencieux" and "arrêté" in e["description"]


def test_etat_tolerant_a_l_intervalle_des_veilles(base, cfg):
    statut(base, maj=T0 - 350)  # veille la plus lente : 120 s → seuil = max(180, 360) = 360 s
    assert embed_etat(cfg, base, T0)["title"] == "🟢 Bot actif"


def test_etat_sans_statut_ou_statut_corrompu(base, cfg):
    assert "jamais démarré" in embed_etat(cfg, base, T0)["description"]
    base.ecrire_meta("statut_bot", "{pas du json")
    assert lire_statut(base) is None and "jamais démarré" in embed_etat(cfg, base, T0)["description"]
    base.ecrire_meta("statut_bot", "[1, 2]")
    assert lire_statut(base) is None


def test_etat_mode_simulation(base, cfg):
    statut(base, dry_run=True)
    assert "--dry-run" in embed_etat(cfg, base, T0)["description"]


# ------------------------------------------------------------------ /stats et achats / ventes


def test_stats_vide(base):
    assert "Aucun achat" in embed_stats(base)["description"]


def test_stats_avec_donnees(base, cfg):
    annonce(base, 1, marge=30.0)
    annonce(base, 2, veille="Ralph Lauren", marge=20.0)
    assert enregistrer_achat(base, cfg, "1", 40.0, T0)[0] and enregistrer_achat(base, cfg, "2", 30.0, T0)[0]
    assert enregistrer_achat(base, cfg, "3", 25.0, T0)[0]  # jamais vue : en stock, sans veille
    assert enregistrer_vente(base, cfg, "1", 70.0, T0 + 5 * JOUR)[0] and enregistrer_vente(base, cfg, "2", 20.0, T0 + 10 * JOUR)[0]
    e = embed_stats(base)
    c = champs(e)
    assert c["Achats"] == "3 (2 revendu(s), 1 en stock)" and c["Capital immobilisé"] == "25,00 €"
    assert c["Marge réelle totale"] == "+20,00 €"  # +30 et −10
    assert c["Délai moyen de revente"] == "7.5 j"
    assert "SURESTIME" in c["Estimation du bot vs réalité (2 article(s))"]  # estimé 25 vs réel 10 en moyenne
    classement = c["Veilles les plus rentables"]
    assert classement.index("Carhartt vestes") < classement.index("Ralph Lauren")
    assert "sans veille" not in classement  # l'article en stock sans veille n'a aucune vente : non listé


def test_enregistrer_achat_et_vente_messages(base, cfg):
    annonce(base, 4001)
    ok, msg = enregistrer_achat(base, cfg, "4001", 36.10, T0)
    assert ok and "36.10 €" in msg and "Carhartt vestes" in msg
    ok, msg = enregistrer_vente(base, cfg, "4001", 70.0, T0 + 3 * JOUR)
    assert ok and "+33.90 €" in msg and "3.0 jour" in msg


def test_erreurs_d_achat_et_de_vente_sont_des_messages_pas_des_exceptions(base, cfg):
    ok, msg = enregistrer_vente(base, cfg, "999", 50.0, T0)
    assert not ok and "Aucun achat" in msg
    ok, msg = enregistrer_achat(base, cfg, "pas-une-annonce", 10.0, T0)
    assert not ok and "identifiant" in msg
    enregistrer_achat(base, cfg, "5", 30.0, T0)
    enregistrer_vente(base, cfg, "5", 50.0, T0 + JOUR)
    ok, msg = enregistrer_achat(base, cfg, "5", 31.0, T0 + 2 * JOUR)  # déjà vendu
    assert not ok and "déjà" in msg
