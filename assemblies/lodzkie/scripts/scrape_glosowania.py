#!/usr/bin/env python3
"""
Radoskop Sejmik Łódzki — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Sejmik Województwa Łódzkiego publikuje sesje przez statyczny eSesja pod
lodzkie.esesja.pl — w przeciwieństwie do większości innych sejmików które
używają app.esesja.pl SPA (zablokowane bez Playwright). Łódzki to jedyny
sejmik gdzie static-HTML eSesja scraper działa od ręki, więc reuse miast.

Skład rady (33 radnych) + przypisania klubowe wczytywane z config.json
(sekcja club_assignments). Wygenerowane z PKW 2024-04-07 (kandydaci
sejmiki wojewodztw).

Backend: https://lodzkie.esesja.pl (Sejmik Łódzki, VII kadencja 2024-2029).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
# assemblies/lodzkie/scripts/ → radoskop/scripts/
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {
    "2024-2029": {"label": "VII kadencja (2024–2029)", "start": "2024-05-07"},
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
        base_url="https://lodzkie.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Sejmik Łódzki (https://lodzkie.esesja.pl)"))
