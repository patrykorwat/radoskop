#!/usr/bin/env python3
"""
Radoskop Tychy — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Council members + club assignments są wczytywane z config.json (sekcja
club_assignments). Format: {"Imię Nazwisko": "kod_klubu"}.
"""

import json
import sys
from pathlib import Path

# Make the shared library importable from monorepo: radoskop/scripts/lib_esesja.py
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}


def _load_councilors() -> dict[str, str]:
    """Czyta club_assignments z config.json (centralne źródło per miasto)."""
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
        base_url="https://tychy.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Tychy (https://tychy.esesja.pl)"))
