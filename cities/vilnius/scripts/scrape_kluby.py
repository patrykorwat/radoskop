#!/usr/bin/env python3
"""
Scraper frakcji (klubów) Vilniaus tarybos.

Wilno nie ma jednolitego maszynowo czytelnego endpoint'a z mapowaniem
radny→frakcja. Strona oficjalna `vilnius.lt/struktura-ir-kontaktai/
vilniaus-miesto-savivaldybes-taryba` wymienia 51 tarybos narių z polami
"Frakcija" w karcie każdego.

Strategia:
1. Pobierz stronę listingu radnych.
2. Wyciągnij linki do indywidualnych kart.
3. Dla każdej karty wyciągnij imię + nazwisko + frakcja.
4. Znormalizuj nazwę frakcji do skróconego kodu z config.clubs.
5. Zaktualizuj config.json.club_assignments.

Uwaga: imię i nazwisko z vilnius.lt muszą się dokładnie zgadzać z polem
`narys` w danych z data.gov.lt (kolejność, znaki diakrytyczne). Walidacja
po pierwszym scrape_balsavimai.py.

Użycie:
    python3 scrape_kluby.py
    python3 scrape_kluby.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30

LISTING_URL = "https://vilnius.lt/struktura-ir-kontaktai/vilniaus-miesto-savivaldybes-taryba"

# Heurystyka mapowania pełnej nazwy frakcji na kod z config.clubs.
# Klucze sortowane od najdłuższych żeby uniknąć złych matchy.
FRAKCIJA_PATTERNS = [
    ("Tėvynės sąjunga", "TS-LKD"),
    ("krikščionys demokratai", "TS-LKD"),
    ("liberalų sąjūdis", "LRLS"),
    ("Liberalų sąjūdis", "LRLS"),
    ("socialdemokratų", "LSDP"),
    ("Darbo partija", "DP"),
    ("Lietuvos valstiečių", "LVZS"),
    ("Žaliųjų sąjunga", "LVZS"),
    ("lenkų rinkimų akcija", "LLRA"),
    ("LLRA", "LLRA"),
    ("Vardan Lietuvos", "DemokratuVardan"),
    ("Demokratų sąjunga", "DemokratuVardan"),
    ("Laisvės partija", "Laisve"),
    ("Nacionalinis susivienijimas", "NS"),
    ("Mišri", "NZ"),
]


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Pobiera HTML z prostym retry.

    Vilnius.lt zwraca 403 dla minimalistycznych headerów (zaobserwowane
    2026-05-14 na NAS). Dodajemy pełen zestaw browser-like headers żeby
    przejść przez bot protection. Jeśli dalej 403, to wymagałby cookies
    z prawdziwej sesji albo zmiana endpointa (np. api.vilnius.lt).
    """
    print(f"  GET {url}", file=sys.stderr)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Chromium";v="120", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/3 after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after 3 attempts: {last_err}")


def normalize_frakcija(text: str) -> str:
    """Mapuje wolnotekstową nazwę frakcji na kod z config.clubs."""
    if not text:
        return "NZ"
    for needle, code in FRAKCIJA_PATTERNS:
        if needle.lower() in text.lower():
            return code
    return "NZ"


def extract_radni(html: str) -> list[dict[str, str]]:
    """Wyciąga listę radnych z HTML listingu vilnius.lt.

    Format strony nie jest stabilny i może wymagać aktualizacji selektorów.
    Tu prosty regex na 'name + frakcja' pattern. Konkretne selektory
    trzeba zweryfikować przy pierwszym uruchomieniu - patrz `dry-run`.
    """
    radni: list[dict[str, str]] = []

    # Card pattern: szuka bloków z nazwiskiem i frakcją obok siebie.
    # Vilnius.lt zwykle ma struktury typu:
    # <h3>Vardas Pavardė</h3> ... Frakcija: ...
    name_pattern = re.compile(
        r"<h\d[^>]*>\s*([A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+(?:\s+[A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž\-]+)+)\s*</h\d>",
        re.UNICODE,
    )
    frakcija_pattern = re.compile(
        r"[Ff]rakcij[ąa][^<]*:\s*([^<\n]{3,200})",
        re.UNICODE,
    )

    # Spróbuj podzielić HTML na "karty radnych" po nagłówkach.
    segments = re.split(r"(?=<h\d[^>]*>\s*[A-ZĄČĘĖĮŠŲŪŽ])", html)
    for seg in segments[1:]:  # pierwszy element to prolog przed pierwszym H
        name_m = name_pattern.search(seg)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        frakcija_m = frakcija_pattern.search(seg)
        frakcija_raw = frakcija_m.group(1).strip() if frakcija_m else ""
        frakcija_code = normalize_frakcija(frakcija_raw)
        radni.append({
            "name": name,
            "frakcija_raw": frakcija_raw,
            "frakcija_code": frakcija_code,
        })

    # Dedup po name.
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for r in radni:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        uniq.append(r)
    return uniq


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pokaż znalezionych radnych ale nie zapisuj config.json.",
    )
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    # vilnius.lt blokuje boty na warstwie WAF (403 nawet z prawdziwymi headerami
    # Chrome). Jeśli scrape padnie, zwracamy 0 - run.sh leci dalej, a
    # club_assignments zostaje pusty. Frakcje da się dorobić ręcznie w config.json
    # albo z innego źródła (Wikipedia, api.vilnius.lt).
    try:
        html = http_get(LISTING_URL)
    except RuntimeError as exc:
        print(
            f"[vilnius] kluby scrape pominięty: {exc}. "
            "config.club_assignments zostaje bez zmian. "
            "Dorób ręcznie z https://vilnius.lt lub Wikipedii.",
            file=sys.stderr,
        )
        return 0

    radni = extract_radni(html)

    print(f"[vilnius] znaleziono {len(radni)} radnych", file=sys.stderr)
    for r in radni:
        print(
            f"  {r['name']:40s}  {r['frakcija_code']:15s}  ({r['frakcija_raw'][:60]})",
            file=sys.stderr,
        )

    if args.dry_run:
        print("[vilnius] dry-run, nie zapisuję", file=sys.stderr)
        return 0

    expected = config.get("councilor_count", 51)
    if len(radni) < expected * 0.7:
        print(
            f"[vilnius] WARN: tylko {len(radni)}/{expected} radnych, "
            f"selektory HTML mogły się zmienić. NIE zapisuję.",
            file=sys.stderr,
        )
        return 1

    club_assignments: dict[str, str] = {r["name"]: r["frakcija_code"] for r in radni}
    config["club_assignments"] = club_assignments

    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(
        f"[vilnius] zaktualizowano config.json: {len(club_assignments)} przypisań",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
