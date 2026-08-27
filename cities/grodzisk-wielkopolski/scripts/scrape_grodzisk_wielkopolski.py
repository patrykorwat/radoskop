#!/usr/bin/env python3
"""Radoskop Grodzisk Wielkopolski — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Backend: https://grodziskwlkp.esesja.pl (Rada Miejska w Grodzisku Wielkopolskim,
IX kadencja 2024-2029). Klasyczny old-template eSesja, serwerowo-renderowane
/listaglosowan + /glosowanie z imiennymi wynikami (.wim/.osobaa).

Uwaga: eSesja slug to `grodziskwlkp` (a nie `grodzisk-wielkopolski` —
grodzisk-wielkopolski.esesja.pl to wildcard-marketingowa strona eSesja).

Skład rady + przypisania klubowe wczytywane z config.json (sekcja
club_assignments). Format: {"Imię Nazwisko": "kod_klubu"}.
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
        base_url="https://grodziskwlkp.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Grodzisk Wielkopolski (https://grodziskwlkp.esesja.pl)"))
