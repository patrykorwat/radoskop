#!/usr/bin/env python3
"""Radoskop Ostrów Mazowiecka — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Źródło: http://ostrowmaz.esesja.pl (UWAGA: subdomen eSesja to 'ostrowmaz',
nie slug miasta). Old-template: /glosowania -> /listaglosowan -> /glosowanie
ze składami imiennymi ZA/PRZECIW/WSTRZYMUJĘ. Zweryfikowane 2026-09-05:
24 sesje IX kad. (newest 2026-07-08), 10+ głosowań/sesja, 21 radnych.

Wcześniejsza wersja (OCR skanów z bip.ostrowmaz.pl) padała w kontenerze na
SSL (CERTIFICATE_VERIFY_FAILED) — eSesja http jest tym samym źródłem raportów
bez OCR i bez problemu certyfikatów.

Skład radnych + przypisania klubowe z config.json (club_assignments).
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
        base_url="http://ostrowmaz.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Ostrów Mazowiecka (http://ostrowmaz.esesja.pl)"))
