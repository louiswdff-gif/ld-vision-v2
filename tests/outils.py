"""Outils de test : fabrique la PAGE HTML d'une recherche Vinted telle que Next.js l'envoie."""

from __future__ import annotations

import json


def page_html(liste: dict, morceaux: int = 3) -> str:
    """Enveloppe `liste` (`{items: [...], pagination: {...}}`) dans une page imitant Next.js : les données sont
    écrites en JSON compact, puis découpées en `morceaux` (en plein milieu des objets, comme le fait le vrai site)
    et placées dans des appels `self.__next_f.push([1,"…"])` dont les chaînes sont échappées."""
    flux = ('5:["$","div",null,{"general":{"title":"$undefined"},"items":'
            + json.dumps(liste, separators=(",", ":"), ensure_ascii=False) + ',"uiState":"SUCCESS"}]\n')
    taille = max(1, len(flux) // morceaux + 1)
    parties = [flux[i:i + taille] for i in range(0, len(flux), taille)]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(p)}])</script>" for p in parties)
    return f"<!DOCTYPE html><html><head><title>Articles | Vinted</title></head><body><div class=\"next-page\"></div>{scripts}</body></html>"
