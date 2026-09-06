#!/usr/bin/env python3
"""Radoskop Międzyrzec Podlaski — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Backend: https://miedzyrzecpodlaski.esesja.pl (Rada Miasta Międzyrzec Podlaski,
IX kadencja 2024-2029; imienne /glosowanie/, sesje tytułowane
'posiedzenie Sesji Rady Miasta ...' — filtr lib_esesja akceptuje prefiks
'posiedzenie' gdy tytuł zawiera rdzeń 'sesj').
club_assignments czytane z config.json (PENDING — kuratorować z BIP).
Dodane 2026-09-06 (cron do 500 miast).
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
        base_url="https://miedzyrzecpodlaski.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Międzyrzec Podlaski (https://miedzyrzecpodlaski.esesja.pl)"))
