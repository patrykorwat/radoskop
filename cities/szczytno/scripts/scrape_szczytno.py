#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Szczytno — AlfaTV 'System Rada' (lib_alfatv).

PUŁAPKA: instancja rada.miastoszczytno.pl zawiera sesje testowe dostawcy
("TEST II", "Test sesji absolutor...", 9 sesji / 75 głosów) — fikcyjne dane,
filtrowane poniżej po nazwie sesji.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_alfatv import AlfTVScraper


class SzczytnoScraper(AlfTVScraper):
    def discover_sessions(self, cache_dir=None):
        sessions = super().discover_sessions(cache_dir)
        keep = [s for s in sessions if not re.match(r"^\s*test\b", s["name"], re.I)]
        dropped = len(sessions) - len(keep)
        if dropped:
            print(f"[szczytno] odrzucono sesji testowych: {dropped}")
        return keep


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    a = ap.parse_args()
    sc = SzczytnoScraper(base_url="https://rada.miastoszczytno.pl", city_label="szczytno")
    raise SystemExit(0 if sc.run(Path(a.city_dir),
                   Path(a.cache_dir) if a.cache_dir else None) else 0)
