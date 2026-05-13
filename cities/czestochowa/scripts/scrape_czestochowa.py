#!/usr/bin/env python3
"""
Radoskop Częstochowa — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Edit COUNCILORS to map councillor names to club codes when you have the data.
Without it, frekwencja/aktywność/votes still work, only club-loyalty stays empty.

Source: https://czestochowa.esesja.pl
"""

import sys
from pathlib import Path

# Make the shared library importable from monorepo: radoskop/scripts/lib_esesja.py
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

# Skład Rady Miasta Częstochowy IX kadencji (25 radnych) wg BIP
# https://bip.czestochowa.pl/artykuly/71763/kluby-radnych (stan 2026-05-13).
# Klub KO ma 8 (oryginalnie 7 + 1 z koalicji), Lewica 6, PiS 9, NZ 2.
# Format: "Imię Nazwisko" → kod klubu. lib_esesja.build_name_lookup obsługuje
# też zamianę kolejności (eSesja używa "Nazwisko Imię").
COUNCILORS: dict[str, str] = {
    # KO — Koalicja Obywatelska (8)
    "Joanna Rekwirewicz": "KO",          # Przewodnicząca Klubu KO
    "Łukasz Banaś": "KO",                 # zawieszony w prawach członka 30.10.2025
    "Marcin Biernat": "KO",               # Przewodniczący Rady Miasta
    "Barbara Gieroń": "KO",
    "Marcin Korzeniec": "KO",
    "Marcin Maranda": "KO",
    "Marta Salwierak": "KO",              # Wiceprzewodnicząca Rady Miasta
    "Zofia Wojtysiak-Kowalik": "KO",      # mandat od 20.06.2024 (po Pabisiu)

    # Lewica (6)
    "Dariusz Kapinos": "Lewica",          # Przewodniczący Klubu Lewicy
    "Zbigniew Niesmaczny": "Lewica",      # Zastępca Przewodniczącego
    "Tomasz Blukacz": "Lewica",
    "Małgorzata Iżyńska": "Lewica",       # Wiceprzewodnicząca Rady Miasta
    "Ewa Lewandowska": "Lewica",          # mandat od 22.01.2026 (po Wolskim)
    "Michał Lewandowski": "Lewica",       # mandat od 26.09.2024 (po Trzeszkowskim)

    # PiS — Prawo i Sprawiedliwość (9)
    "Paweł Ruksza": "PiS",                # Przewodniczący Klubu PiS
    "Monika Pohorecka-Całko": "PiS",      # Wiceprzewodnicząca Rady Miasta
    "Katarzyna Jastrzębska": "PiS",       # Sekretarz Klubu PiS
    "Robert Leciński": "PiS",
    "Alan Piotrowski": "PiS",
    "Karolina Stępień": "PiS",
    "Beata Struzik": "PiS",
    "Artur Warzocha": "PiS",
    "Piotr Wrona": "PiS",

    # NZ — radni niezrzeszeni (2)
    "Krystyna Stefańska": "NZ",
    "Krzysztof Świerczyński": "NZ",
}

if __name__ == "__main__":
    raise SystemExit(EsesjaScraper(
        base_url="https://czestochowa.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Częstochowa (https://czestochowa.esesja.pl)"))
