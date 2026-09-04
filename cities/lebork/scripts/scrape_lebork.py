#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Lębork — AlfaTV 'System Rada' (lib_alfatv)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_alfatv import AlfTVScraper

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    a = ap.parse_args()
    sc = AlfTVScraper(base_url="https://rada.um.lebork.pl", city_label="lebork")
    raise SystemExit(0 if sc.run(Path(a.city_dir),
                   Path(a.cache_dir) if a.cache_dir else None) else 0)
