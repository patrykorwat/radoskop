#!/usr/bin/env python3
"""
Scraper budżetu MHMP (Magistrát hl. m. Prahy).

Praha publikuje budżet w open-data jako czyste CSV per rok, na hoście
opendata-storage.praha.eu (poza WAF F5 ASM). Dwa źródła per rok:

  Rozpočet MHMP {YYYY}     plan budżetu (rozpocet_schvaleny, rozpocet_upraveny)
    https://opendata-storage.praha.eu/ROZ_rozpocet_finance/rozpocet/Zdroje/{Y}/Rozpocet_MHMP_{Y}.csv

  Čerpání rozpočtu MHMP {Y} wykonanie (rozpocet_aktualni, cerpani)
    https://opendata-storage.praha.eu/UCT_cerpani_rozpoctu/cerpani_rozpoctu/Zdroje/{Y}/Cerpani_rozpoctu_MHMP_{Y}.csv

Schema CSV:
    rok;oblast;odpa;pol;uz;nazev_oblast;nazev_odpa;nazev_pol;nazev_uz;rozpocet_*;cerpani

Klucze ekonomiczne (pol):
    1xxx daňové příjmy           → revenue
    2xxx nedaňové příjmy         → revenue
    3xxx kapitálové příjmy       → revenue (capital income)
    4xxx přijaté transfery       → revenue (grants)
    5xxx běžné výdaje            → expenditure (operating)
    6xxx kapitálové výdaje       → expenditure (capital / investments)
    8xxx financování             → financing (deficit, debt)

Wartości w CSV: tisíce Kč (×1000 żeby otrzymać Kč).

Schema output (kompatybilna z polskimi miastami):
    {
      "totals": [{"year": 2025, "revenue": 80000000000, "expenditure": ...,
                  "deficit": ..., "estimated": false}],
      "categories": {"2025": [{"name": "Transport", "amount": 18000000000}, ...]},
      "investments": {"2025": {"total": ..., "categories": [...]}},
      "votes": {}    -- nie podpinamy specjalnych głosowań nad budżetem na razie
    }

Nazwy kategorii: dla locale=en mapujemy czeskie nazev_oblast → angielski.
Lista standardowa: Transport, Education, Social services, Healthcare,
Security, Environment, Municipal services, Public administration,
Culture & sport, Housing, Other.

Użycie:
    python3 scrape_budget.py
    python3 scrape_budget.py --years 2024,2025,2026
    python3 scrape_budget.py --no-cache
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache" / "budget"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30

# URL templates per rok. Praga publikuje plan i wykonanie pod ścieżkami z
# parametrem roku, ale niektóre lata mają dodatkowy subfolder z miesiącem
# (np. 2023/12/, 2024/12/, 2025/01/) — pewnie efekt wersjonowania snapshot.
# Próbujemy plain "/year/" najpierw, potem "/year/12/" i "/year/01/".
ROZPOCET_URL_VARIANTS = [
    "https://opendata-storage.praha.eu/ROZ_rozpocet_finance/rozpocet/Zdroje/{year}/Rozpocet_MHMP_{year}.csv",
    "https://opendata-storage.praha.eu/ROZ_rozpocet_finance/rozpocet/Zdroje/{year}/12/Rozpocet_MHMP_{year}.csv",
    "https://opendata-storage.praha.eu/ROZ_rozpocet_finance/rozpocet/Zdroje/{year}/01/Rozpocet_MHMP_{year}.csv",
]
CERPANI_URL_VARIANTS = [
    "https://opendata-storage.praha.eu/UCT_cerpani_rozpoctu/cerpani_rozpoctu/Zdroje/{year}/Cerpani_rozpoctu_MHMP_{year}.csv",
    "https://opendata-storage.praha.eu/UCT_cerpani_rozpoctu/cerpani_rozpoctu/Zdroje/{year}/12/Cerpani_rozpoctu_MHMP_{year}.csv",
    "https://opendata-storage.praha.eu/UCT_cerpani_rozpoctu/cerpani_rozpoctu/Zdroje/{year}/01/Cerpani_rozpoctu_MHMP_{year}.csv",
]

# Mapowanie czeskich nazw oblast → kategorie Radoskop (locale=en).
# Lista pochodzi z faktycznych nazev_oblast w danych; pozycje nie pasujące
# trafiają do "Other".
CATEGORY_MAP_EN = {
    "Doprava": "Transport",
    "Školství": "Education",
    "Školství a vzdělávání": "Education",
    "Sociální služby": "Social services",
    "Sociální věci a politika zaměstnanosti": "Social services",
    "Zdravotnictví": "Healthcare",
    "Bezpečnost a veřejný pořádek": "Security",
    "Životní prostředí": "Environment",
    "Komunální služby": "Municipal services",
    "Komunální služby a územní rozvoj": "Municipal services",
    "Bydlení": "Housing",
    "Bydlení, komunální služby a územní rozvoj": "Housing",
    "Veřejná správa": "Public administration",
    "Veřejná správa a služby pro obyvatelstvo": "Public administration",
    "Kultura": "Culture & sport",
    "Kultura, církve a sdělovací prostředky": "Culture & sport",
    "Tělovýchova a zájmová činnost": "Culture & sport",
    "Sport": "Culture & sport",
    "Zemědělství": "Agriculture",
    "Zemědělství a lesní hospodářství": "Agriculture",
    "Průmysl": "Industry",
    "Průmysl a ostatní odvětví hospodářství": "Industry",
    "Přijaté transfery": "Transfers in",
    "Financování": "Financing",
}


def http_download(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT,
                 quiet: bool = False) -> bool:
    """Pobiera URL do pliku. Zwraca True przy sukcesie."""
    try:
        if not quiet:
            print(f"  GET {url}", file=sys.stderr)
        req = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv,*/*",
        })
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if not quiet:
            print(f"  saved {len(data)/1024:.1f} KB → {dest}", file=sys.stderr)
        return True
    except (HTTPError, URLError) as exc:
        if not quiet:
            print(f"  miss {url} ({exc})", file=sys.stderr)
        return False


def http_download_first(urls: list[str], dest: Path, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """Próbuje listę URL-i, kończy na pierwszym sukcesie."""
    for url in urls:
        if http_download(url, dest, timeout=timeout, quiet=True):
            print(f"  ✓ {url} → {dest.name}", file=sys.stderr)
            return True
    print(f"  ✗ all variants failed for {dest.name}", file=sys.stderr)
    return False


def parse_amount(s: str) -> float:
    """CSV ma wartości w tisíce Kč jako '12345.67' albo '-12.50'."""
    s = (s or "").strip()
    if not s or s == "0.00":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def map_category_en(nazev: str) -> str:
    """Mapuj czeskie nazev_oblast → angielską kategorię Radoskop."""
    if not nazev:
        return "Other"
    if nazev in CATEGORY_MAP_EN:
        return CATEGORY_MAP_EN[nazev]
    # Heurystyka fallback: dopasowanie po prefiksach.
    n = nazev.lower()
    for cz, en in CATEGORY_MAP_EN.items():
        if n.startswith(cz.lower().split()[0]):
            return en
    return "Other"


def parse_csv(path: Path) -> list[dict[str, str]]:
    """Czyta CSV z BOM, separator `;`, zwraca listę dict."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(text.splitlines(), delimiter=";")
    return [row for row in reader]


def aggregate_year(year: int, rozpocet_csv: Path | None,
                  cerpani_csv: Path | None) -> dict[str, Any]:
    """Z CSV rocznego buduje totals + categories + investments dla roku."""
    revenue_total = 0.0
    expenditure_total = 0.0
    investments_total = 0.0
    by_category: dict[str, float] = defaultdict(float)
    investments_by_category: dict[str, float] = defaultdict(float)

    # Rozpocet to plan, cerpani to wykonanie. Preferujemy cerpani gdy jest,
    # fallback na rozpocet_upraveny.
    rows: list[dict[str, str]] = []
    use_cerpani = False
    if cerpani_csv and cerpani_csv.exists():
        rows = parse_csv(cerpani_csv)
        use_cerpani = True
    elif rozpocet_csv and rozpocet_csv.exists():
        rows = parse_csv(rozpocet_csv)

    if not rows:
        return {}

    for row in rows:
        pol = (row.get("pol") or "").strip()
        if not pol:
            continue
        # Wartość: cerpani jeśli plik wykonania, inaczej rozpocet_upraveny.
        if use_cerpani:
            amount_thsk = parse_amount(row.get("cerpani") or row.get("rozpocet_aktualni") or "0")
        else:
            amount_thsk = parse_amount(
                row.get("rozpocet_upraveny") or row.get("rozpocet_schvaleny") or "0"
            )
        if amount_thsk == 0:
            continue
        amount_kc = amount_thsk * 1000  # tisíce → Kč

        pol_first = pol[:1]
        nazev = (row.get("nazev_oblast") or "").strip()

        # Klasyfikacja po pol prefix:
        if pol_first in ("1", "2", "3", "4"):
            revenue_total += amount_kc
        elif pol_first == "5":
            # Bieżące wydatki
            expenditure_total += amount_kc
            cat = map_category_en(nazev)
            by_category[cat] += amount_kc
        elif pol_first == "6":
            # Kapitałowe wydatki = inwestycje. Liczymy do expenditure i osobno.
            expenditure_total += amount_kc
            investments_total += amount_kc
            cat = map_category_en(nazev)
            by_category[cat] += amount_kc
            investments_by_category[cat] += amount_kc
        # 8xxx financování: pomijamy w totals; te kwoty nie są wydatkami,
        # tylko ruchami salda (zaciągnięcie kredytu, splata).

    # Top 11 kategorii (jak warszawa). Reszta agreguje się do "Other".
    sorted_cats = sorted(by_category.items(), key=lambda x: -x[1])
    top_n = 11
    if len(sorted_cats) > top_n:
        rest = sum(amount for _, amount in sorted_cats[top_n - 1:])
        sorted_cats = sorted_cats[:top_n - 1] + [("Other", rest)]

    invest_sorted = sorted(investments_by_category.items(), key=lambda x: -x[1])

    deficit = expenditure_total - revenue_total

    return {
        "totals": {
            "year": year,
            "revenue": int(round(revenue_total)),
            "expenditure": int(round(expenditure_total)),
            "deficit": int(round(deficit)),
            "estimated": not use_cerpani,
        },
        "categories": [
            {"name": name, "amount": int(round(amount))}
            for name, amount in sorted_cats
        ],
        "investments": {
            "total": int(round(investments_total)),
            "categories": [
                {"name": name, "amount": int(round(amount))}
                for name, amount in invest_sorted[:11]
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--years", default=None,
                        help="Comma-separated lista lat (np. 2024,2025,2026). "
                             "Default: ostatnie 5 lat z config.budget_years.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=None,
                        help="Plik wyjściowy. Default: docs/budget.json")
    parser.add_argument("--no-cache", action="store_true",
                        help="Zignoruj cache, zawsze pobierz świeży CSV.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"[budget] brak config: {config_path}", file=sys.stderr)
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))

    if args.years:
        years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    else:
        years = config.get("budget_years")
        if not years:
            current = datetime.now().year
            years = list(range(current - 4, current + 1))

    cache_dir = Path(args.cache_dir)

    totals_list: list[dict[str, Any]] = []
    categories_by_year: dict[str, list[dict[str, Any]]] = {}
    investments_by_year: dict[str, dict[str, Any]] = {}

    for year in years:
        print(f"[budget] year={year}", file=sys.stderr)
        rozpocet_path = cache_dir / f"rozpocet_{year}.csv"
        cerpani_path = cache_dir / f"cerpani_{year}.csv"

        if args.no_cache or not rozpocet_path.exists():
            http_download_first(
                [u.format(year=year) for u in ROZPOCET_URL_VARIANTS],
                rozpocet_path,
            )
        if args.no_cache or not cerpani_path.exists():
            http_download_first(
                [u.format(year=year) for u in CERPANI_URL_VARIANTS],
                cerpani_path,
            )

        agg = aggregate_year(
            year,
            rozpocet_path if rozpocet_path.exists() else None,
            cerpani_path if cerpani_path.exists() else None,
        )
        if not agg:
            print(f"  no data for {year}, skipping", file=sys.stderr)
            continue

        totals_list.append(agg["totals"])
        categories_by_year[str(year)] = agg["categories"]
        investments_by_year[str(year)] = agg["investments"]

    if not totals_list:
        print("[budget] no years yielded data, skipping budget.json write", file=sys.stderr)
        return 0

    output = {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": "Kč",
        "totals": totals_list,
        "categories": categories_by_year,
        "investments": investments_by_year,
        "votes": {},  # głosowania nad budżetem dorzucone osobno (przyszła iteracja)
    }

    output_path = Path(args.output) if args.output else DEFAULT_DOCS / "budget.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[budget] zapisano {output_path}", file=sys.stderr)
    print(f"  lat: {len(totals_list)}", file=sys.stderr)
    print(f"  kategorii w ostatnim roku: {len(categories_by_year[str(years[-1])])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
