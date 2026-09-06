#!/usr/bin/env python3
"""Radoskop Gryfów Śląski — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

UWAGA: eSesja działa pod NIESPÓJNYM slugiem 'gryfowslaski' (bez łącznika), nie
'gryfow-slaski' — {slug}.esesja.pl dla slugu miasta to wildcard (wraca stronę
marketingową eSesja.pl).

Skład rady + przypisania klubowe są wczytywane z config.json (sekcja
club_assignments). Format: {"Imię Nazwisko": "kod_klubu"}.

Backend: https://gryfowslaski.esesja.pl (Rada Miejska Gminy Gryfów Śląski,
IX kadencja 2024-2029). Dodane automatycznie w ramach ekspansji 500 miast 2026-09-01.
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
        base_url="https://gryfowslaski.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Gryfów Śląski (https://gryfowslaski.esesja.pl)"))
