"""Bot Discord interactif : commandes /stats, /veilles, /top, /etat, /acheter, /vendu et boutons sur les alertes.

- `logique.py` : tout le contenu (embeds, textes, contrôles) sans dépendance à discord.py → testable seul ;
- `bot.py`     : la couche discord.py (connexion, commandes, boutons, formulaires, canal d'alertes).

Rappel : le bot ne fait que détecter et suivre. Le bouton d'achat est un simple LIEN vers l'annonce Vinted ;
aucun achat, message ou offre n'est jamais envoyé automatiquement.
"""
