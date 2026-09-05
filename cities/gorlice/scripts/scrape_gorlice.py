#!/usr/bin/env python3
"""Radoskop Gorlice — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Backend: https://gorlice.esesja.pl (Rada Miasta w Gorlicach, IX kadencja
2024-2029; PM-instance A — struktura old-template, imienne /glosowanie/).
UWAGA: portal publikuje mala liczbe sesji biezacej kadencji (sesja 2025-10-16,
12 glosowan imiennych) — przyrostowo dojdzie wiecej.
club_assignments czytane z config.json (PENDING — kuratorować z BIP).
Dodane 2026-09-05 (cron do 500 miast).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}


def _load_councilors() -> dict[str, str]:
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


COUNCILORS = _load_councilors()

if __name__ == "__main__":
    raise SystemExit(EsesjaScraper(
        base_url="https://gorlice.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Gorlice (https://gorlice.esesja.pl)"))
