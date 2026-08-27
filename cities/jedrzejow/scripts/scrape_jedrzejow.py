#!/usr/bin/env python3
"""Radoskop Jędrzejów — eSesja scraper (subclass of scripts/lib_esesja.py).

Backend: https://jedrzejow.esesja.pl (Rada Miejska w Jędrzejowie, IX kadencja
2024-2029). Publikuje głosowania przez eSesja "Portal Mieszkańca" instance pod
slugiem `jedrzejow`; strony sesji są serwerowo-renderowane w strukturze
old-template (PM-instance A), więc istniejący parser lib_esesja wyciąga
imienne głosowania bez zmian.

Subklasa naprawia dwa specyficzne problemy źródła Jędrzejowa:
 1) Nazwiska złożone złamane na linii ("Maciąg - Wojtanowska Agnieszka",
    "Zmarzły - Prokop Izabela") → sklejane do "Maciąg-Wojtanowska Agnieszka"
    zanim bazowy swap "Nazwisko Imię" → "Imię Nazwisko" ustawi je jako
    "Agnieszka Maciąg-Wojtanowska" / "Izabela Zmarzły-Prokop".
 2) Kluby default NZ (BIP Jędrzejowa nie publikuje klubów radnych).

Skład: 24 radnych IX kadencji (22 obecnych wg BIP "Skład Rady Miejskiej" +
dwaj wcześni radni rotacji: Karolina Jarosz, Robert Kruk — obecni w sesjach
2024-05..2024-10). Dodane automatycznie w ramach ekspansji cities 2026-08-27.
"""

import json
import re
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


class JedrzejowScraper(EsesjaScraper):
    def _normalize_name(self, raw: str) -> str:
        if not raw:
            return raw
        raw = raw.strip()
        # Napraw złamane nazwisko złożone: "Maciąg - Wojtanowska Agnieszka" ->
        # "Maciąg-Wojtanowska Agnieszka" zanim bazowy swap. Toleruje spacje wokół
        # myślnika i warianty "X- Y" / "X -Y".
        raw = re.sub(
            r"\b([\wąęłńóśźż]+)\s*-\s*([\wąęłńóśźż]+)(?=\s+\S)",
            r"\1-\2",
            raw,
        )
        raw = re.sub(
            r"\b([\wąęłńóśźż]+)\s*-\s*([\wąęłńóśźż]+)$",
            r"\1-\2",
            raw,
        )
        return super()._normalize_name(raw)

    def resolve_club(self, name: str) -> str:
        return super().resolve_club(name) or "NZ"


if __name__ == "__main__":
    raise SystemExit(JedrzejowScraper(
        base_url="https://jedrzejow.esesja.pl",
        kadencje=KADENCJE,
        councilors=_load_councilors(),
    ).run_cli(prog_name="Radoskop Jędrzejów (https://jedrzejow.esesja.pl)"))
