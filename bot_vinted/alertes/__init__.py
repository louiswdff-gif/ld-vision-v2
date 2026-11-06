"""Canaux d'alerte : console (--dry-run), Discord (webhook), Telegram (optionnel)."""

from bot_vinted.alertes.base import Alerte, Canal, Diffuseur, ErreurEnvoi, construire_diffuseur

__all__ = ["Alerte", "Canal", "Diffuseur", "ErreurEnvoi", "construire_diffuseur"]
