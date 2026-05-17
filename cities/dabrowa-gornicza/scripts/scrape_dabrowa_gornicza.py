#!/usr/bin/env python3
"""
Radoskop Dąbrowa Górnicza — eSesja scraper.

Council members + club assignments są wczytywane z config.json.

Backend: https://dabrowagornicza.esesja.pl/ (Rada Miejska w Dąbrowie Górniczej,
IX kadencja 2024-2029, 25 radnych). Rozkład: DR 9, KO 7, PiS 5, TPDG 3, DIS 1.
Skład z portalsamorzadowy.pl/miasto/dabrowa-gornicza,425.html.
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
        base_url="https://dabrowagornicza.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Dąbrowa Górnicza (https://dabrowagornicza.esesja.pl)"))
