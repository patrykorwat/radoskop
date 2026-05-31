#!/usr/bin/env python3
"""
Scraper frakcji Дніпровської міської ради.

Dніпро publikuje frakcje w datasecie radnych na data.dniprorada.gov.ua.
Kolumna `factionName` zawiera pełną nazwę frakcji (np. "Фракція партії
«Слуга народу»"). Skrypt pobiera deputies CSV, mapuje nazwy frakcji
na kody z config.clubs, i aktualizuje config.json.club_assignments.

Przy pierwszym uruchomieniu po scrape_ckan_ua.py frakcje są automatyczne.
Jeśli scraper pobierze nowe frakcje nieznane w mapowaniu → lądują jako NZ.

Użycie:
  python3 scrape_kluby.py
  python3 scrape_kluby.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
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

# Mapowanie fragmentów nazw frakcji na kody z config.clubs.
# Klucze case-insensitive, kolejność: od najdłuższych dla bezpiecznego matchu.
FACTION_PATTERNS: list[tuple[str, str]] = [
    ("слуга народу", "SN"),
    ("слуга", "SN"),
    ("опозиційна платформа", "OPZZh"),
    ("опозиційна", "OPZZh"),
    ("за майбутнє", "ZaMaybutne"),
    ("батьківщина", "Batkivshchyna"),
    ("рідне місто", "RidneMisto"),
    ("самопоміч", "Samopomich"),
    ("свобода", "Svoboda"),
    ("євросолідарність", "YES"),
    ("голос", "Holos"),
    ("позафракційний", "NZ"),
    ("нефракційний", "NZ"),
    ("нефракційна", "NZ"),
]


def http_get(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed: {last_err}")


def normalize_faction(text: str) -> str:
    if not text:
        return "NZ"
    lower = text.lower()
    for needle, code in FACTION_PATTERNS:
        if needle in lower:
            return code
    return "NZ"


def _resources_from_html(page_url: str) -> list[dict[str, str]]:
    """HTML fallback: wyciąga linki /download/*.csv ze strony datasetu."""
    import re as _re
    raw = http_get(page_url, timeout=30)
    html = raw.decode("utf-8", errors="replace")
    pattern = _re.compile(
        r'href=["\']([^"\'?#]*?/download/([^"\'?#/\s]+\.csv))["\']',
        _re.IGNORECASE,
    )
    base = page_url.split("/dataset/")[0]
    seen: set[str] = set()
    resources = []
    for m in pattern.finditer(html):
        href, filename = m.group(1), m.group(2)
        url = href if href.startswith("http") else f"{base}{href}"
        if url not in seen:
            seen.add(url)
            resources.append({"url": url, "name": filename, "format": "CSV"})
    return resources


def fetch_deputies_csv(
    ckan_base: str,
    deputies_id: str,
    browse_url: str | None = None,
    html_first: bool = False,
) -> list[dict[str, str]]:
    """Pobiera deputies CSV.

    html_first=True albo API timeout → HTML fallback z browse_url.
    """
    def _resources_from_api() -> list[dict]:
        api_url = f"{ckan_base}/api/3/action/package_show?id={deputies_id}"
        raw = http_get(api_url, timeout=15)
        pkg = json.loads(raw)
        if not pkg.get("success"):
            raise RuntimeError(f"CKAN error: {pkg}")
        return pkg["result"]["resources"]

    if html_first and browse_url:
        resources = _resources_from_html(browse_url)
    else:
        try:
            resources = _resources_from_api()
        except Exception as exc:
            if not browse_url:
                raise
            print(f"  WARN: API niedostępne ({exc}), HTML fallback", file=sys.stderr)
            resources = _resources_from_html(browse_url)

    deputies_res = [
        r for r in resources
        if r.get("name", "").lower().startswith("deputies")
        and r.get("format", "").upper() == "CSV"
    ]
    if not deputies_res:
        raise RuntimeError("Brak zasobu deputies CSV")
    latest = sorted(deputies_res, key=lambda r: r.get("name", ""), reverse=True)[0]
    raw_csv = http_get(latest["url"], timeout=60)
    text = raw_csv.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    ckan_base = config.get("ckan_base", "https://data.dniprorada.gov.ua")
    deputies_id = config.get("ckan_deputies_id")
    if not deputies_id:
        print("[dnipro] WARN: brak ckan_deputies_id w config, pomijam", file=sys.stderr)
        return 0

    try:
        deputies = fetch_deputies_csv(ckan_base, deputies_id)
    except Exception as exc:
        print(f"[dnipro] WARN: nie można pobrać radnych: {exc}", file=sys.stderr)
        return 0

    print(f"[dnipro] znaleziono {len(deputies)} radnych", file=sys.stderr)

    # Zbierz unikalne frakcje
    factions_seen: set[str] = set()
    assignments: dict[str, str] = {}
    for dep in deputies:
        surname = dep.get("familyName", "").strip()
        first = dep.get("name", "").strip()
        patronymic = dep.get("additionalName", "").strip()
        full_name = " ".join(filter(None, [surname, first, patronymic]))
        if not full_name:
            continue
        faction_raw = dep.get("factionName", "").strip()
        factions_seen.add(faction_raw)
        code = normalize_faction(faction_raw)
        assignments[full_name] = code

    print("[dnipro] frakcje w danych:", file=sys.stderr)
    for f in sorted(factions_seen):
        code = normalize_faction(f)
        print(f"  {code:20s}  {f}", file=sys.stderr)

    if args.dry_run:
        print("[dnipro] dry-run, nie zapisuję", file=sys.stderr)
        return 0

    expected = config.get("councilor_count", 64)
    if len(assignments) < expected * 0.5:
        print(
            f"[dnipro] WARN: tylko {len(assignments)}/{expected} radnych. NIE zapisuję.",
            file=sys.stderr,
        )
        return 1

    config["club_assignments"] = assignments

    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(
        f"[dnipro] zapisano config.json: {len(assignments)} przypisań",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
