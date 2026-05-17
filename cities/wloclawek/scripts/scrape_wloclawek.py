#!/usr/bin/env python3
"""
Radoskop Włocławek — eSesja scraper.

Council members + club assignments są wczytywane z config.json.

Backend: https://wloclawek.esesja.pl/ (Rada Miasta Włocławek, IX kadencja
2024-2029, 23 radnych). Rozkład: KO 9, Lewica 8, PiS 6.
Skład z portalsamorzadowy.pl/miasto/wloclawek,430.html.
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
        base_url="https://wloclawek.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Włocławek (https://wloclawek.esesja.pl)"))
