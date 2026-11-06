"""Faits du protocole Vinted (identifiants et libellés).

Ce ne sont PAS des réglages utilisateur : ce sont des constantes imposées par Vinted
(ex. l'identifiant 6 = « Neuf avec étiquette »). Si Vinted les change, on corrige ici.
"""

from __future__ import annotations

import re

# Valeurs internes de l'état d'un article (clés utilisées dans config.yaml) → status_ids Vinted.
ETATS_IDS: dict[str, int] = {
    "neuf_avec_etiquette": 6,
    "neuf_sans_etiquette": 1,
    "tres_bon_etat": 2,
    "bon_etat": 3,
    "satisfaisant": 4,
}
IDS_ETATS: dict[int, str] = {v: k for k, v in ETATS_IDS.items()}

# Libellés renvoyés par l'API selon la langue du site, déjà normalisés (voir texte.normaliser).
# À compléter si un domaine renvoie un libellé inconnu (le bot l'écrit alors dans le journal).
LIBELLES_ETATS: dict[str, str] = {
    # français
    "neuf avec etiquette": "neuf_avec_etiquette",
    "neuf sans etiquette": "neuf_sans_etiquette",
    "tres bon etat": "tres_bon_etat",
    "bon etat": "bon_etat",
    "satisfaisant": "satisfaisant",
    # anglais
    "new with tags": "neuf_avec_etiquette",
    "new without tags": "neuf_sans_etiquette",
    "very good": "tres_bon_etat",
    "good": "bon_etat",
    "satisfactory": "satisfaisant",
    # espagnol
    "nuevo con etiquetas": "neuf_avec_etiquette",
    "nuevo sin etiquetas": "neuf_sans_etiquette",
    "muy bueno": "tres_bon_etat",
    "bueno": "bon_etat",
    "satisfactorio": "satisfaisant",
    # italien
    "nuovo con cartellino": "neuf_avec_etiquette",
    "nuovo senza cartellino": "neuf_sans_etiquette",
    "ottime condizioni": "tres_bon_etat",
    "buone condizioni": "bon_etat",
    "condizioni soddisfacenti": "satisfaisant",
    # allemand
    "neu mit etikett": "neuf_avec_etiquette",
    "neu ohne etikett": "neuf_sans_etiquette",
    "sehr gut": "tres_bon_etat",
    "gut": "bon_etat",
    "zufriedenstellend": "satisfaisant",
}

ETATS_VALIDES = tuple(ETATS_IDS)

# Un nom de domaine Vinted : vinted.fr, vinted.be, vinted.co.uk, vinted.com…
MOTIF_DOMAINE = re.compile(r"^vinted\.[a-z]{2,3}(?:\.[a-z]{2})?$")


def normaliser_domaine(valeur: str) -> str:
    """« https://www.vinted.fr/catalog?… » ou « www.Vinted.fr » → « vinted.fr »."""
    v = valeur.strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = v.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    if not MOTIF_DOMAINE.match(v):
        raise ValueError(f"domaine Vinted invalide : « {valeur} » (attendu : vinted.fr, vinted.be, vinted.es…)")
    return v
