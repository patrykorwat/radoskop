#!/usr/bin/env python3
"""Radoskop Rada Powiatu Tucholskiego: eSesja scraper (cienki wrapper na lib_esesja).

Wszystkie parametry (esesja_url, kadencje, club_assignments) z config.json,
plik jest identyczny dla każdego powiatu i generowany przez
radoskop-premium/scripts/scaffold_district.py. Wzorzec: assemblies/lodzkie.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
# districts/{slug}/scripts/ -> radoskop/scripts/
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper  # noqa: E402

CFG = json.loads((HERE.parent.parent / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    raise SystemExit(EsesjaScraper(
        base_url=CFG["esesja_url"],
        kadencje=CFG["kadencje"],
        councilors=CFG.get("club_assignments", {}) or {},
    ).run_cli(prog_name=f"Radoskop {CFG['rada_name']} ({CFG['esesja_url']})"))
