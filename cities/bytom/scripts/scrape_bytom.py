#!/usr/bin/env python3
"""
Radoskop Bytom — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Bytom eSesja stores names as "Nazwisko Imię" (surname first), hence
name_order="swap_surname_first" — lib_esesja will flip to "Imię Nazwisko"
for display and slug generation.

Skład IX kadencji (2024-2029):
  KO  — Koalicja Obywatelska (14 mandatów)
  PIS — Prawo i Sprawiedliwość (8 mandatów)
  WB2050 — Wspólny Bytom 2050 / Trzecia Droga (3 mandaty)

Źródło: wyniki wyborów 2024 + BIP bytom.pl
Uwagi:
  - Panek Dominika = Sobczak Dominika (zmiana nazwiska po ślubie), klub PIS
  - Niewiadomski Krzysztof: następca Biedy Michała (KO) po rezygnacji z mandatu
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

# Nazwy w formacie eSesja: "Nazwisko Imię"
COUNCILORS: dict[str, str] = {
    # KO — Koalicja Obywatelska
    "Adamczyk-Nowak Beata":  "KO",
    "Bula Piotr":            "KO",
    "Gajos Maciej":          "KO",
    "Jabłoński Tymoteusz":   "KO",
    "Koloch Magdalena":      "KO",
    "Kozak Izabela":         "KO",
    "Napierała Michał":      "KO",
    "Niewiadomski Krzysztof": "KO",
    "Nowak Grzegorz":        "KO",
    "Pakosz Iwona":          "KO",
    "Polak Teresa":          "KO",
    "Staniszewski Michał":   "KO",
    "Stępień Joanna":        "KO",
    "Wadowska Barbara":      "KO",
    # PIS — Prawo i Sprawiedliwość
    "Bartków Maciej":        "PIS",
    "Dąbrowski Dawid":       "PIS",
    "Gawron Waldemar":       "PIS",
    "Janas Mariusz":         "PIS",
    "Jaszczak Kamil":        "PIS",
    "Panek Dominika":        "PIS",
    "Patoń Piotr":           "PIS",
    "Pyrk Alfred":           "PIS",
    # WB2050 — Wspólny Bytom 2050 (wybrani z listy Trzeciej Drogi)
    "Krieser Witold":        "WB2050",
    "Probierz Kamil":        "WB2050",
    "Wężyk Andrzej":         "WB2050",
}

if __name__ == "__main__":
    raise SystemExit(EsesjaScraper(
        base_url="https://bytom.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
        name_order="swap_surname_first",
    ).run_cli(prog_name="Radoskop Bytom (https://bytom.esesja.pl)"))
