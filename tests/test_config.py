from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bot_vinted.config import ErreurConfig, charger_config
from bot_vinted.journal import FiltreSecrets


def ecrire(tmp_path: Path, contenu: str) -> Path:
    chemin = tmp_path / "config.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


def test_config_exemple_est_valide(exemple_config_path):
    cfg = charger_config(exemple_config_path)
    assert cfg.domaine == "vinted.fr"
    assert cfg.frais_acheteur.fixe == 0.70 and cfg.frais_acheteur.pourcentage == 5
    assert [v.nom for v in cfg.veilles] == ["Carhartt vestes", "Ralph Lauren pulls"]


def test_valeurs_par_defaut_heritees_par_les_veilles(exemple_config_path):
    cfg = charger_config(exemple_config_path)
    carhartt, ralph = cfg.veilles
    assert carhartt.port_estime == 6.5 and carhartt.marge_min == 20 and carhartt.score_min == 60
    assert carhartt.webhook == "DISCORD_WEBHOOK_CARHARTT"
    assert carhartt.intervalle_secondes == 60  # hérité du global
    assert ralph.webhook == "DISCORD_WEBHOOK_URL"  # webhook par défaut
    assert ralph.port_estime == 5.0  # defauts_veille


def test_config_minimale(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "veilles:\n  - nom: Test\n    mots_cles: [nike]\n"))
    assert cfg.revente.decote == 0.85 and cfg.revente.min_references == 10
    assert cfg.score.seuils.feu == 80 and cfg.score.seuils.ok == 60
    assert "contrefacons" in cfg.exclusions_globales.model_dump()


def test_intervalle_global_inferieur_a_30_refuse(tmp_path):
    with pytest.raises(ErreurConfig, match="minimum 30 s"):
        charger_config(ecrire(tmp_path, "intervalle_secondes: 10\n"))


def test_intervalle_de_veille_inferieur_a_30_refuse(tmp_path):
    texte = "veilles:\n  - nom: A\n    mots_cles: [x]\n    intervalle_secondes: 29\n"
    with pytest.raises(ErreurConfig, match="minimum 30 s"):
        charger_config(ecrire(tmp_path, texte))


def test_intervalle_de_30_secondes_accepte(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "veilles:\n  - nom: A\n    mots_cles: [x]\n    intervalle_secondes: 30\n"))
    assert cfg.veilles[0].intervalle_secondes == 30


def test_cle_inconnue_est_une_erreur(tmp_path):
    with pytest.raises(ErreurConfig, match="clé inconnue"):
        charger_config(ecrire(tmp_path, "intervale_secondes: 60\n"))  # faute de frappe


def test_etat_invalide_refuse(tmp_path):
    texte = "veilles:\n  - nom: A\n    mots_cles: [x]\n    etats: [comme_neuf]\n"
    with pytest.raises(ErreurConfig, match="etats"):
        charger_config(ecrire(tmp_path, texte))


def test_noms_de_veille_en_double_refuses(tmp_path):
    texte = "veilles:\n  - {nom: A, mots_cles: [x]}\n  - {nom: A, mots_cles: [y]}\n"
    with pytest.raises(ErreurConfig, match="en double"):
        charger_config(ecrire(tmp_path, texte))


def test_veille_sans_aucun_critere_refusee(tmp_path):
    with pytest.raises(ErreurConfig, match="au moins"):
        charger_config(ecrire(tmp_path, "veilles:\n  - nom: Vide\n"))


def test_prix_min_superieur_a_prix_max_refuse(tmp_path):
    texte = "veilles:\n  - {nom: A, mots_cles: [x], prix_min: 50, prix_max: 10}\n"
    with pytest.raises(ErreurConfig, match="prix_min"):
        charger_config(ecrire(tmp_path, texte))


def test_domaine_normalise_et_valide(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "domaine: https://www.Vinted.be/catalog\n"))
    assert cfg.domaine == "vinted.be"
    with pytest.raises(ErreurConfig, match="domaine"):
        charger_config(ecrire(tmp_path, "domaine: exemple.com\n"))


def test_fichier_absent(tmp_path):
    with pytest.raises(ErreurConfig, match="introuvable"):
        charger_config(tmp_path / "nexiste-pas.yaml")


def test_yaml_invalide(tmp_path):
    with pytest.raises(ErreurConfig, match="YAML invalide"):
        charger_config(ecrire(tmp_path, "veilles: [\n"))


def test_secrets_lus_dans_dotenv_et_signales_si_absents(tmp_path, monkeypatch):
    texte = "veilles:\n  - {nom: A, mots_cles: [x], webhook: MON_WEBHOOK}\n"
    chemin = ecrire(tmp_path, texte)
    monkeypatch.delenv("MON_WEBHOOK", raising=False)
    cfg = charger_config(chemin)
    assert cfg.secrets_manquants() == ["DISCORD_WEBHOOK_URL", "MON_WEBHOOK"]

    (tmp_path / ".env").write_text("MON_WEBHOOK=https://discord.com/api/webhooks/1/abc\n", encoding="utf-8")
    monkeypatch.delenv("MON_WEBHOOK", raising=False)
    cfg = charger_config(chemin)
    assert cfg.secrets_manquants() == ["DISCORD_WEBHOOK_URL"]
    assert cfg.secret("MON_WEBHOOK") == "https://discord.com/api/webhooks/1/abc"
    monkeypatch.delenv("MON_WEBHOOK", raising=False)


def test_chemins_relatifs_resolus_depuis_le_dossier_de_config(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "base: {chemin: donnees/x.db}\n"))
    assert cfg.chemin(cfg.base.chemin) == tmp_path.resolve() / "donnees" / "x.db"


def test_telegram_ajoute_ses_variables_seulement_si_actif(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "telegram: {actif: false}\n"))
    assert "TELEGRAM_BOT_TOKEN" not in cfg.variables_secretes()
    cfg = charger_config(ecrire(tmp_path, "telegram: {actif: true}\n"))
    assert {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} <= set(cfg.variables_secretes())


# ----- masquage des secrets dans les logs -----


def _enregistrement(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)


def test_filtre_masque_les_valeurs_connues_et_les_motifs():
    secret = "https://discord.com/api/webhooks/123456/AbCdEfGhIjKl"
    filtre = FiltreSecrets([secret])
    rec = _enregistrement(f"envoi vers {secret} puis https://discord.com/api/webhooks/999/AUTRE_TOKEN")
    filtre.filter(rec)
    assert "AbCdEfGhIjKl" not in rec.msg and "AUTRE_TOKEN" not in rec.msg
    assert "***" in rec.msg


def test_filtre_masque_le_token_telegram_dans_une_url():
    filtre = FiltreSecrets()
    rec = _enregistrement("POST https://api.telegram.org/bot123456:ABC-def_ghi/sendPhoto")
    filtre.filter(rec)
    assert "ABC-def_ghi" not in rec.msg


# ----- CLI -----


def test_cli_verifier(exemple_config_path, capsys):
    from bot_vinted.cli import principal

    code = principal(["verifier", "--config", str(exemple_config_path)])
    sortie = capsys.readouterr().out
    assert code == 0
    assert "Carhartt vestes" in sortie and "DISCORD_WEBHOOK_CARHARTT" in sortie


def test_cli_sans_sous_commande_equivaut_a_run():
    from bot_vinted.cli import _avec_commande_par_defaut, construire_parseur

    argv = _avec_commande_par_defaut(["--dry-run"])
    assert argv == ["run", "--dry-run"]
    assert construire_parseur().parse_args(argv).dry_run is True
    assert _avec_commande_par_defaut(["stats"]) == ["stats"]
    assert _avec_commande_par_defaut(["--help"]) == ["--help"]


def test_cli_prix_accepte_virgule_et_refuse_negatif():
    from bot_vinted.cli import construire_parseur

    p = construire_parseur()
    assert p.parse_args(["acheter", "123", "25,50"]).prix == 25.5
    with pytest.raises(SystemExit):
        p.parse_args(["acheter", "123", "-5"])


def test_cli_config_introuvable(capsys, tmp_path):
    from bot_vinted.cli import principal

    assert principal(["verifier", "--config", str(tmp_path / "absent.yaml")]) == 1
    assert "introuvable" in capsys.readouterr().err


# ----- bot Discord interactif -----


def test_bot_discord_inactif_par_defaut_secrets_inchanges(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "veilles:\n  - {nom: A, mots_cles: [x]}\n"))
    assert cfg.discord_bot.actif is False
    assert cfg.variables_secretes() == ["DISCORD_WEBHOOK_URL"]


def test_bot_discord_actif_remplace_les_webhooks_par_son_token(tmp_path):
    texte = ("discord_bot: {actif: true, salon_id: 111, serveur_id: 222, utilisateurs_autorises: [333]}\n"
             "veilles:\n  - {nom: A, mots_cles: [x], webhook: MON_WEBHOOK, salon_discord: 444}\n")
    cfg = charger_config(ecrire(tmp_path, texte))
    assert cfg.discord_bot.utilisateurs_autorises == [333]
    assert cfg.veilles[0].salon_discord == 444
    assert cfg.variables_secretes() == ["DISCORD_BOT_TOKEN"]  # plus de webhook requis
    assert cfg.secrets_manquants() == ["DISCORD_BOT_TOKEN"]


def test_bot_discord_actif_exige_un_salon(tmp_path):
    with pytest.raises(ErreurConfig, match="salon_id"):
        charger_config(ecrire(tmp_path, "discord_bot: {actif: true}\n"))


def test_bot_discord_avec_telegram_cumule_les_secrets(tmp_path):
    cfg = charger_config(ecrire(tmp_path, "discord_bot: {actif: true, salon_id: 1}\ntelegram: {actif: true}\n"))
    assert set(cfg.variables_secretes()) == {"DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
