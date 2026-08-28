#!/usr/bin/env python3
"""Radoskop Krosno Odrzańskie — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Backend: https://krosnoodrzanskie.esesja.pl (Rada Miejska w Krośnie Odrzańskim,
IX kadencja 2024-2029). Publikuje głosowania przez eSesja "Portal Mieszkańca"
instance pod slugiem `krosnoodrzanskie` (a nie `krosno-odrzanskie` —
krosno-odrzanskie.esesja.pl to wildcard-marketingowa strona eSesja). Template
detect_template() zwraca portal-mieszkanca, ale strony sesji są serwerowo-
renderowane w strukturze old-template, więc istniejący parser lib_esesja
wyciąga imienne głosowania bez zmian.

Skład rady + przypisania klubowe wczytywane z config.json (sekcja club_assignments).
Dodane automatycznie w ramach ekspansji cities 2026-08-28.
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
        base_url="https://krosnoodrzanskie.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Krosno Odrzańskie (https://krosnoodrzanskie.esesja.pl)"))
