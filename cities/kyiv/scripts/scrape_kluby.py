#!/usr/bin/env python3
"""
Scraper frakcji Київської міської ради.

Kyiv nie ma osobnego datasetu CKAN z frakcjami. Źródło: strona rady
kmr.gov.ua/uk/deputies — lista radnych z polem frakcji.

Jeśli scrape się nie uda (WAF, 403) — frakcje pozostają puste (NZ).
Można też uzupełnić ręcznie w config.json.club_assignments.

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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEPUTIES_URL = "https://kmr.gov.ua/uk/deputies"

# Mapowanie fragmentów nazw frakcji → kody z config.clubs
FACTION_PATTERNS: list[tuple[str, str]] = [
    ("блок кличка", "BK"),
    ("кличка", "BK"),
    ("слуга народу", "SN"),
    ("євросолідарність", "YES"),
    ("батьківщина", "Batkivshchyna"),
    ("самопоміч", "Samopomich"),
    ("голос", "Holos"),
    ("позафракційний", "NZ"),
    ("нефракційний", "NZ"),
]


def http_get(url: str, timeout: int = 30) -> str:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    })
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed: {last_err}")


def normalize_faction(text: str) -> str:
    lower = text.lower()
    for needle, code in FACTION_PATTERNS:
        if needle in lower:
            return code
    return "NZ"


def extract_deputies(html: str) -> list[dict[str, str]]:
    """Wyciąga radnych z HTML kmr.gov.ua/uk/deputies."""
    deputies: list[dict[str, str]] = []
    # Szukamy bloków: imię + frakcja. Format strony nie jest stabilny.
    # Pattern: znajdź pary nazwisko+frakcja w kartach deputat.
    name_re = re.compile(
        r"<[^>]+class=\"[^\"]*(?:deputy|depname|full.?name)[^\"]*\"[^>]*>\s*"
        r"([А-ЯІЇЄA-Z][а-яіїєa-zА-ЯІЇЄ\s\.\-]+)\s*</",
        re.IGNORECASE | re.UNICODE,
    )
    faction_re = re.compile(
        r"(?:фракц|фракція|фракции)[^<]*:\s*([^<\n]{3,150})",
        re.IGNORECASE | re.UNICODE,
    )
    segments = re.split(r"(?=<(?:div|article|li)[^>]+class=\"[^\"]*deput)", html)
    for seg in segments[1:]:
        name_m = name_re.search(seg)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        faction_m = faction_re.search(seg)
        faction_raw = faction_m.group(1).strip() if faction_m else ""
        code = normalize_faction(faction_raw)
        deputies.append({"name": name, "faction_code": code, "faction_raw": faction_raw})
    seen: set[str] = set()
    unique = []
    for d in deputies:
        if d["name"] not in seen:
            seen.add(d["name"])
            unique.append(d)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    try:
        html = http_get(DEPUTIES_URL)
    except RuntimeError as exc:
        print(
            f"[kyiv] WARN: scrape frakcji nieudany: {exc}. "
            "club_assignments pozostaje bez zmian.",
            file=sys.stderr,
        )
        return 0

    deputies = extract_deputies(html)
    print(f"[kyiv] znaleziono {len(deputies)} radnych", file=sys.stderr)
    for d in deputies:
        print(f"  {d['faction_code']:15s}  {d['name']}", file=sys.stderr)

    if args.dry_run:
        print("[kyiv] dry-run, nie zapisuję", file=sys.stderr)
        return 0

    if not deputies:
        print("[kyiv] WARN: 0 radnych — NIE zapisuję", file=sys.stderr)
        return 0

    assignments = {d["name"]: d["faction_code"] for d in deputies}
    config["club_assignments"] = assignments

    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"[kyiv] zapisano {len(assignments)} przypisań frakcji", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
