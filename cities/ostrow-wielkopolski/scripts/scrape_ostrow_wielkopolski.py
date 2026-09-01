#!/usr/bin/env python3
"""
Radoskop Ostrów Wielkopolski — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: https://ostrowmiasto.esesja.pl (eSesja Portal Mieszkańca, PM-instance A —
server-renderowany /glosowania -> /listaglosowan/{id} -> /glosowanie/{id}/{hash} sformatem
starego template'a). Skrapianie przez współdzielony lib_esesja.EsesjaScraper.
BIP: bip.umostrow.pl (link "Głosowania" -> ostrowmiasto.esesja.pl/glosowania).
Kluby: PENDING (brak oficjalnego wykazu klubów po stronie serwera BIP — kuratorować później).
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
        base_url="https://ostrowmiasto.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Ostrów Wielkopolski (https://ostrowmiasto.esesja.pl)"))
