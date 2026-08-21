#!/usr/bin/env python3
"""Radoskop Złotoryja — interpelacje/zapytania (eSesja https://zlotoryja.esesja.pl).
Thin wrapper around scripts/lib_esesja_interp.py (IX kad. 2024-2029).
Użycie: python3 scrape_interpelacje.py --output docs/interpelacje.json
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja_interp import make_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(make_main("zlotoryja", "https://zlotoryja.esesja.pl")())
