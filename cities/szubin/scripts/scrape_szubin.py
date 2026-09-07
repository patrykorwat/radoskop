#!/usr/bin/env python3
"""Radoskop Szubin — scraper platformy posiedzenia.pl (wrapper scrape_karczew.PosiedzeniaPlScraper).

BIP Gminy Szubin (bip.szubin.pl, kategoria 'Wykazy głosowań radnych') kieruje
"Głosowania Radnych kadencja 2024-2029" na portal https://szubin.posiedzenia.pl .
Portal identyczny jak Karczew (dodane 2026-09-07) — używamy wspólnej klasy
PosiedzeniaPlScraper (headless playwright /admin/zawartosc.php O3/O7).

Dodane 2026-09-07 (cron do 500 miast). club_assignments pending.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
sys.path.insert(0, str(HERE.parents[2] / "karczew" / "scripts"))

from scrape_karczew import PosiedzeniaPlScraper  # noqa: E402

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


if __name__ == "__main__":
    sc = PosiedzeniaPlScraper(
        kadencje=KADENCJE,
        councilors=_load_councilors(),
        portal="https://szubin.posiedzenia.pl",
    )
    try:
        rc = sc.run_cli(prog_name="Radoskop Szubin (posiedzenia.pl)")
    finally:
        sc.close()
    raise SystemExit(rc)
