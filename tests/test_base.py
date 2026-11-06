from __future__ import annotations

import pytest

from bot_vinted.base import Base


@pytest.fixture
def base():
    with Base(":memory:") as b:
        yield b


def annonce_retenue(**kw) -> dict:
    v = dict(
        domaine="vinted.fr", annonce_id=4001, veille="Carhartt vestes", titre="Veste Carhartt", marque="Carhartt",
        taille="L", etat="tres_bon_etat", url="https://www.vinted.fr/items/4001", photo_url="https://img/4001.jpg",
        prix=28.0, cout_reel=36.1, revente_estimee=70.0, source_revente="cible", nb_references=3, marge=33.9,
        roi=93.9, score=78, tags="🔤 marque mal orthographiée", publie_le=1731000000.0, vendeur_note=4.8,
        vendeur_avis=142, alerte_statut="en_attente", alerte_le=None, cree_le=1731000100.0,
    )
    v.update(kw)
    return v


# ------------------------------------------------------------------ persistance


def test_creation_dossier_et_persistance_sur_disque(tmp_path):
    chemin = tmp_path / "donnees" / "sous" / "bot.db"
    with Base(chemin) as b:
        b.marquer_vue("vinted.fr", 1, "V", 100.0)
    with Base(chemin) as b:  # réouverture : les données sont toujours là
        assert b.est_vue("vinted.fr", 1)
        assert b._lire_un("SELECT valeur FROM meta WHERE cle = 'version_schema'")["valeur"] == "1"


# ------------------------------------------------------------------ veilles et dédoublonnage


def test_premier_passage_puis_passages_suivants(base):
    assert base.etat_veille("V") is None  # jamais exécutée → « premier lancement »
    base.marquer_passage("V", 1000.0)
    base.marquer_passage("V", 1060.0)
    etat = base.etat_veille("V")
    assert etat["premier_passage_le"] == 1000.0  # inchangé
    assert etat["derniere_execution_le"] == 1060.0


def test_dedoublonnage(base):
    assert base.est_vue("vinted.fr", 4001) is False
    assert base.marquer_vue("vinted.fr", 4001, "V", 1000.0) is True  # nouvelle
    assert base.marquer_vue("vinted.fr", 4001, "V", 1001.0) is False  # déjà vue
    assert base.marquer_vue("vinted.fr", 4001, "AUTRE_VEILLE", 1002.0) is False  # même annonce, autre veille
    assert base.est_vue("vinted.fr", 4001)
    assert base.est_vue("vinted.be", 4001) is False  # un même id sur un autre domaine est une autre annonce


# ------------------------------------------------------------------ historique de prix


def test_historique_une_seule_fois_par_annonce(base):
    assert base.enregistrer_prix("vinted.fr", 1, "carhartt", "2050", "l", "bon_etat", 30.0, 1000.0) is True
    assert base.enregistrer_prix("vinted.fr", 1, "carhartt", "2050", "l", "bon_etat", 99.0, 1001.0) is False
    assert base.prix_references("vinted.fr", "carhartt", "2050", "l", "bon_etat", 0) == [30.0]


def test_references_filtrees_par_cle_fenetre_et_exclusion(base):
    for i, (marque, taille, etat, prix, t) in enumerate([
        ("carhartt", "l", "bon_etat", 30.0, 1000.0),
        ("carhartt", "l", "bon_etat", 40.0, 2000.0),
        ("carhartt", "l", "bon_etat", 50.0, 3000.0),
        ("carhartt", "m", "bon_etat", 99.0, 3000.0),  # autre taille
        ("carhartt", "l", "neuf_avec_etiquette", 99.0, 3000.0),  # autre état
        ("nike", "l", "bon_etat", 99.0, 3000.0),  # autre marque
    ]):
        base.enregistrer_prix("vinted.fr", i, marque, "2050", taille, etat, prix, t)
    base.enregistrer_prix("vinted.be", 100, "carhartt", "2050", "l", "bon_etat", 99.0, 3000.0)  # autre domaine

    assert sorted(base.prix_references("vinted.fr", "carhartt", "2050", "l", "bon_etat", 0)) == [30.0, 40.0, 50.0]
    # fenêtre : on ne garde que les prix vus depuis t=1500
    assert sorted(base.prix_references("vinted.fr", "carhartt", "2050", "l", "bon_etat", 1500)) == [40.0, 50.0]
    # l'annonce évaluée ne sert pas de référence à elle-même
    assert sorted(base.prix_references("vinted.fr", "carhartt", "2050", "l", "bon_etat", 0, exclure_id=1)) == [30.0, 50.0]


def test_purge(base):
    base.enregistrer_prix("vinted.fr", 1, "m", "c", "t", "e", 10.0, 100.0)
    base.enregistrer_prix("vinted.fr", 2, "m", "c", "t", "e", 10.0, 900.0)
    base.marquer_vue("vinted.fr", 1, "V", 100.0)
    base.marquer_vue("vinted.fr", 2, "V", 900.0)
    assert base.purger(avant=500.0) == (1, 1)
    assert base.est_vue("vinted.fr", 1) is False and base.est_vue("vinted.fr", 2) is True
    assert base.prix_references("vinted.fr", "m", "c", "t", "e", 0) == [10.0]


# ------------------------------------------------------------------ annonces et statut d'alerte


def test_annonce_retenue_et_statut_d_alerte(base):
    assert base.enregistrer_annonce(annonce_retenue()) is True
    assert base.enregistrer_annonce(annonce_retenue(score=99)) is False  # jamais deux fois la même annonce
    assert base.annonce("vinted.fr", 4001)["score"] == 78

    en_attente = base.alertes_en_attente()
    assert [r["annonce_id"] for r in en_attente] == [4001]

    base.marquer_alerte("vinted.fr", 4001, "envoyee", 1731000200.0)
    assert base.alertes_en_attente() == []
    ligne = base.annonce("vinted.fr", 4001)
    assert ligne["alerte_statut"] == "envoyee" and ligne["alerte_le"] == 1731000200.0


def test_alertes_en_attente_ordonnees_et_limitees(base):
    for i, t in [(1, 300.0), (2, 100.0), (3, 200.0)]:
        base.enregistrer_annonce(annonce_retenue(annonce_id=i, cree_le=t))
    base.enregistrer_annonce(annonce_retenue(annonce_id=4, cree_le=50.0, alerte_statut="simulee"))  # dry-run : exclue
    assert [r["annonce_id"] for r in base.alertes_en_attente()] == [2, 3, 1]
    assert [r["annonce_id"] for r in base.alertes_en_attente(limite=2)] == [2, 3]


def test_annonce_valeurs_optionnelles_absentes(base):
    v = annonce_retenue(marque=None, taille=None, tags=None, nb_references=None, vendeur_note=None, vendeur_avis=None,
                        photo_url=None, publie_le=None)
    assert base.enregistrer_annonce(v) is True
    ligne = base.annonce("vinted.fr", 4001)
    assert ligne["marque"] == "" and ligne["tags"] == "" and ligne["vendeur_note"] is None


def test_annonces_par_id(base):
    base.enregistrer_annonce(annonce_retenue(domaine="vinted.fr"))
    base.enregistrer_annonce(annonce_retenue(domaine="vinted.be"))
    assert {r["domaine"] for r in base.annonces_par_id(4001)} == {"vinted.fr", "vinted.be"}


# ------------------------------------------------------------------ achats


def test_achat_puis_vente(base):
    base.enregistrer_achat("vinted.fr", 4001, "Carhartt vestes", "Veste", 36.1, 1000.0, 33.9)
    assert base.achat("vinted.fr", 4001)["prix_vente_net"] is None
    base.enregistrer_vente("vinted.fr", 4001, 75.0, 1000.0 + 5 * 86400)
    achat = base.achat("vinted.fr", 4001)
    assert achat["prix_vente_net"] == 75.0 and achat["date_vente"] == 1000.0 + 5 * 86400
    assert [a["annonce_id"] for a in base.tous_les_achats()] == [4001]


def test_reachat_avant_vente_corrige_le_prix(base):
    base.enregistrer_achat("vinted.fr", 1, "V", "T", 30.0, 1000.0, 10.0)
    base.enregistrer_achat("vinted.fr", 1, "V", "T", 32.0, 2000.0, 10.0)  # correction
    achat = base.achat("vinted.fr", 1)
    assert achat["prix_achat_total"] == 32.0 and achat["date_achat"] == 1000.0  # date d'origine conservée
    assert len(base.tous_les_achats()) == 1


def test_achat_deja_vendu_non_modifiable(base):
    base.enregistrer_achat("vinted.fr", 1, "V", "T", 30.0, 1000.0, None)
    base.enregistrer_vente("vinted.fr", 1, 50.0, 2000.0)
    with pytest.raises(ValueError, match="déjà"):
        base.enregistrer_achat("vinted.fr", 1, "V", "T", 31.0, 3000.0, None)


def test_vente_sans_achat_refusee(base):
    with pytest.raises(ValueError, match="aucun achat"):
        base.enregistrer_vente("vinted.fr", 999, 50.0, 1000.0)
