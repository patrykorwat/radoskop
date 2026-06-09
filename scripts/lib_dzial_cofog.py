#!/usr/bin/env python3
"""Mapowanie polskich działów klasyfikacji budżetowej na funkcje COFOG.

COFOG = Classification of the Functions of Government (10 funkcji, GF01..GF10),
ta sama którą stosuje Eurostat. Pozwala pokazać strukturę wydatków miasta wg
funkcji i porównać z innymi miastami tą samą metodą.

Większość działów mapuje się 1:1 (działy PL są funkcjonalne). Dwa są SPLIT i
wymagają rozdziału: 900 (gospodarka komunalna GF06 + ochrona środowiska GF05) i
925 (ogrody/obszary chronione). Dla tych dwóch używamy nadpisań per rozdział.

GUS robi oficjalny pomost na poziomie rozdziału do raportowania Eurostatowi; to
jest przybliżenie wystarczające do wizualizacji struktury (udziały %), nie do
twierdzeń co do grosza. Patrz radoskop-premium/strategia/COFOG_BENCHMARK.md.
"""
from __future__ import annotations

COFOG_LABELS: dict[str, str] = {
    "GF01": "Administracja i usługi ogólne",
    "GF02": "Obrona",
    "GF03": "Bezpieczeństwo i porządek publiczny",
    "GF04": "Sprawy gospodarcze (transport, rolnictwo, energia)",
    "GF05": "Ochrona środowiska",
    "GF06": "Gospodarka mieszkaniowa i komunalna",
    "GF07": "Zdrowie",
    "GF08": "Rekreacja, kultura i religia",
    "GF09": "Edukacja",
    "GF10": "Ochrona socjalna",
}

# Dział (3 cyfry) -> COFOG. None = pomijamy (np. dział czysto dochodowy).
DZIAL_COFOG: dict[str, str] = {
    "010": "GF04", "020": "GF04", "050": "GF04", "100": "GF04", "150": "GF04",
    "400": "GF04",                      # energia/gaz/woda (dominanta energia)
    "500": "GF04", "550": "GF04", "600": "GF04", "630": "GF04",
    "700": "GF06", "710": "GF06",       # mieszkalnictwo, geodezja/planowanie
    "720": "GF01", "730": "GF01",       # informatyka, nauka
    "750": "GF01", "751": "GF01", "756": "GF01", "757": "GF01", "758": "GF01",
    "752": "GF02",
    "753": "GF10",                      # obowiązkowe ubezpieczenia społeczne
    "754": "GF03", "755": "GF03",       # bezpieczeństwo/ppoż, wymiar sprawiedliwości
    "801": "GF09", "803": "GF09", "854": "GF09",
    "851": "GF07",
    "852": "GF10", "853": "GF10", "855": "GF10",
    "921": "GF08", "926": "GF08",
    # 900 i 925: SPLIT -> patrz overrides po rozdziale (default niżej).
}

# Rozdziały działu 900 (gospodarka komunalna i ochrona środowiska):
# odpady/ścieki/środowisko -> GF05, komunalne/oświetlenie/zieleń -> GF06.
ROZDZIAL_900: dict[str, str] = {
    "90001": "GF05",  # gospodarka ściekowa i ochrona wód
    "90002": "GF05",  # gospodarka odpadami komunalnymi
    "90003": "GF06",  # oczyszczanie miast i wsi
    "90004": "GF05",  # utrzymanie zieleni
    "90005": "GF05",  # ochrona powietrza i klimatu
    "90013": "GF05",  # schroniska dla zwierząt
    "90015": "GF06",  # oświetlenie ulic, placów i dróg
    "90017": "GF06",  # zakłady gospodarki komunalnej
    "90019": "GF05",  # ochrona środowiska (wpływy/wydatki)
    "90020": "GF05",  # wpływy z opłat produktowych
    "90026": "GF05",  # pozostałe działania ochrony środowiska
    "90095": "GF06",  # pozostała działalność
}
DEFAULT_900 = "GF06"
DEFAULT_925 = "GF05"  # ogrody/obszary chronione -> ochrona przyrody


def map_to_cofog(dzial: str | None, rozdzial: str | None = None) -> str | None:
    """Zwraca kod COFOG (GF01..GF10) dla pary (dział, rozdział) lub None.

    Dla działów 900 i 925 używa rozdziału (split GF05/GF06). Dla reszty wystarcza
    sam dział.
    """
    if not dzial:
        return None
    d = str(dzial).strip().zfill(3)[:3]
    r = str(rozdzial).strip().zfill(5)[:5] if rozdzial else ""
    if d == "900":
        return ROZDZIAL_900.get(r, DEFAULT_900)
    if d == "925":
        return DEFAULT_925
    return DZIAL_COFOG.get(d)
