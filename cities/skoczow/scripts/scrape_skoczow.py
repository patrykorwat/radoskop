#!/usr/bin/env python3
"""Radoskop Skoczów — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Backend: https://skoczow.esesja.pl (Rada Miejska w Skoczowie,
IX kadencja 2024-2029). Publikuje głosowania przez eSesja "Portal Mieszkańca"
instance pod slugiem `skoczow` — serwerowo-renderowane sesje (PM-instance A,
old-compatible), więc istniejący parser lib_esesja wyciąga imienne głosowania
bez zmian (25 sesji IX kad., 309 głosowań, 21 radnych - 2026-08-27).

Skład rady + przypisania klubowe wczytywane z config.json (sekcja club_assignments).
Dodane automatycznie w ramach ekspansji cities 2026-08-27.
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
        base_url="https://skoczow.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Skoczów (https://skoczow.esesja.pl)"))
