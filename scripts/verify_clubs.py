#!/usr/bin/env python3
"""
Weryfikator przypisań radnych do klubów — porównuje dane w kodzie z BIP.

Użycie:
    python3 verify_clubs.py                          # wszystkie miasta
    python3 verify_clubs.py --city wroclaw            # tylko jedno
    python3 verify_clubs.py --report-only             # tylko raport, bez zapisu

Co robi:
    Dla każdego miasta ściąga stronę klubów z BIP, wyciąga listę radnych,
    porównuje z COUNCILORS / club_assignments i wypisuje różnice.

Stan: WERSJA POCZĄTKOWA — wspiera miasta, których BIP udało się zmapować.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CITIES_DIR = HERE.parent / "cities"
HEADERS = {"User-Agent": "Radoskop/1.0 (https://radoskop.pl; verify_clubs.py)"}
TIMEOUT = 30


# ---------------------------------------------------------------------------
# City configuration: how to fetch and parse each city's BIP clubs page
# ---------------------------------------------------------------------------

@dataclass
class CityConfig:
    """Configuration for one city's club verification."""
    slug: str                              # directory name under cities/
    name: str                              # display name
    url: str                               # BIP clubs page URL
    source_type: str = "councilors"        # "councilors" (inline dict) or "config" (club_assignments)
    # How to find club sections on the page
    club_heading_selector: str = "h2"      # HTML tag for club headings
    club_member_selector: str = None       # How to find members after a heading
    # Custom parser function name (if None, use generic)
    parser: str | None = None
    # For cities where clubs are in a table with a "Klub" column
    table_selector: str | None = None
    club_column: str | None = None


# Known city configurations
# Each entry: how to find the BIP clubs page and extract data
CITY_CONFIGS: list[CityConfig] = [
    CityConfig(
        slug="wroclaw",
        name="Wrocław",
        url="https://bip.um.wroc.pl/artykul/1187/73078/kluby-radnych",
        source_type="councilors",
    ),
    CityConfig(
        slug="katowice",
        name="Katowice",
        url="https://bip.katowice.eu/Strony/Kluby_radnych.aspx?menu=658",
        source_type="councilors",
    ),
    CityConfig(
        slug="lublin",
        name="Lublin",
        url="https://bip.lublin.eu/rada-miasta-lublin/ix-kadencja/radni-rady-miasta-lublin/",
        source_type="councilors",
    ),
    CityConfig(
        slug="szczecin",
        name="Szczecin",
        url="https://bip.um.szczecin.pl/chapter_50591.asp",
        source_type="councilors",
        parser="szczecin_table",
    ),
    CityConfig(
        slug="torun",
        name="Toruń",
        url="https://bip.torun.pl/artykuly/32529/kluby-radnych",
        source_type="config",
    ),
    CityConfig(
        slug="kielce",
        name="Kielce",
        url="https://bipum.kielce.eu/rada-miasta-kielce/kluby-radnych/",
        source_type="config",
    ),
    CityConfig(
        slug="olsztyn",
        name="Olsztyn",
        url="https://bip.olsztyn.eu/20/radni-2024-2029.html",
        source_type="councilors",
        parser="olsztyn_list",
    ),
    CityConfig(
        slug="bydgoszcz",
        name="Bydgoszcz",
        url="https://bip.um.bydgoszcz.pl/artykul/1473/5809/kadencja-ix-2024-2029",
        source_type="councilors",
    ),
    CityConfig(
        slug="czestochowa",
        name="Częstochowa",
        url="https://bip.czestochowa.pl/artykuly/71763/kluby-radnych",
        source_type="councilors",
    ),
    CityConfig(
        slug="lodz",
        name="Łódź",
        url="https://bip.uml.lodz.pl/wladze/rada-miejska-w-lodzi/kluby-radnych-ix-kadencji/",
        source_type="councilors",
    ),
    CityConfig(
        slug="poznan",
        name="Poznań",
        url="https://bip.poznan.pl/bip/sesje/",
        source_type="councilors",
        parser="poznan",
    ),
]


# ---------------------------------------------------------------------------
# Generic parser: BIP pages with <h2>Club Name</h2> followed by member list
# ---------------------------------------------------------------------------

def parse_club_headings(soup: BeautifulSoup, config: CityConfig) -> dict[str, list[str]]:
    """Parse clubs from <h2> headings followed by member lists."""
    clubs: dict[str, list[str]] = {}
    current_club = None

    for el in soup.find_all(["h2", "h3", "h4", "p", "li", "div"]):
        tag = el.name
        text = el.get_text(" ", strip=True)

        # Detect club headings
        if tag in ("h2", "h3", "h4"):
            lower = text.lower()
            if "klub" in lower or "niezrzesz" in lower or "radni" in lower:
                # Extract club name
                club_name = _extract_club_name(text)
                if club_name:
                    current_club = club_name
                    if club_name not in clubs:
                        clubs[club_name] = []
                    continue

        # Detect member names after a club heading
        if current_club is not None and tag in ("p", "li", "div"):
            name = _extract_member_name(text)
            if name:
                clubs[current_club].append(name)

    return clubs


def _extract_club_name(text: str) -> str | None:
    """Extract short club name from heading like 'Klub Radnych Koalicja Obywatelska:'"""
    text = re.sub(r'\s+', ' ', text).strip().rstrip(':')
    lower = text.lower()

    if "niezrzesz" in lower:
        return "Niezrzeszeni"

    m = re.match(r'(?:klub\s+radnych\s+)?(.+)', text, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        # Map common names to short codes
        name_lower = name.lower()
        if "koalicja obywatelska" in name_lower:
            return "KO"
        if "prawo i sprawiedliwość" in name_lower:
            return "PiS"
        if "lewica naprzód" in name_lower or "lewica" in name_lower:
            return "Lewica Naprzód" if "naprzód" in name_lower else "Lewica"
        if "naprawmy przyszłość" in name_lower:
            return "Naprawmy Przyszłość"
        if "wspólny toruń" in name_lower:
            return "WT"
        if "trzecia droga" in name_lower:
            return "Trzecia Droga"
        if "bydgoska prawica" in name_lower:
            return "Bydgoska Prawica"
        if "ko i lewica" in name_lower:
            return "KO i Lewica"
        if "forum" in name_lower:
            return "Forum"
        return name
    return None


def _extract_member_name(text: str) -> str | None:
    """Extract councilor name from a line of text."""
    # Remove common prefixes/suffixes
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[-–—]\s*(Przewodniczący|Wiceprzewodniczący|Radny|Radna).*', '', text).strip()
    text = re.sub(r'[-–—]\s*funkcja.*', '', text).strip()
    text = re.sub(r'\s*\(.*?\)\s*', ' ', text).strip()

    # Must have at least 2 words (first + last name)
    parts = text.split()
    if len(parts) < 2:
        return None
    if len(text) > 60:
        return None
    if any(ch.isdigit() for ch in text):
        return None

    # Check if it looks like a name (starts with uppercase)
    if not parts[0][0].isupper():
        return None

    return text


# ---------------------------------------------------------------------------
# Custom parsers for specific BIP formats
# ---------------------------------------------------------------------------

def parse_szczecin_table(soup: BeautifulSoup, config: CityConfig) -> dict[str, list[str]]:
    """Szczecin BIP: table with 'Klub' column."""
    clubs: dict[str, list[str]] = {}
    table = soup.find("table")
    if not table:
        return clubs

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        name_cell = cells[0].get_text(" ", strip=True)
        role_cell = cells[1].get_text(" ", strip=True)
        club_cell = cells[2].get_text(" ", strip=True)
        if not name_cell or not club_cell:
            continue
        # Skip header row
        if any(w in name_cell.lower() for w in ["imię", "nazwisko"]):
            continue
        # Normalize name: "Nazwisko Imię" → "Imię Nazwisko"
        # Clean up non-breaking spaces
        name_cell = name_cell.replace('\xa0', ' ').replace('\\xa0', ' ')
        name_cell = re.sub(r'\s+', ' ', name_cell).strip()
        # Remove status annotations like "(rezygnacja)", "(wygaśnięcie mandatu)"
        name_cell = re.sub(r'\s*\([^)]*\)\s*', '', name_cell).strip()
        parts = name_cell.split()
        if len(parts) >= 2:
            name = f"{parts[-1]} {' '.join(parts[:-1])}"
        else:
            name = name_cell
        # Normalize club name
        club = _normalize_club_name(club_cell)
        if club not in clubs:
            clubs[club] = []
        clubs[club].append(name)
    return clubs


def _normalize_club_name(name: str) -> str:
    """Map full BIP club names to short codes."""
    mapping = {
        "koalicja obywatelska": "KO",
        "prawo i sprawiedliwość": "PiS",
        "ok polska": "OK",
        "lewica naprzód": "Lewica Naprzód",
        "naprawmy przyszłość": "Naprawmy Przyszłość",
        "wspólny toruń": "WT",
        "trzecia droga": "Trzecia Droga",
        "bydgoska prawica": "Bydgoska Prawica",
        "ko i lewica": "KO i Lewica",
        "niezrzeszeni": "Niezrzeszeni",
        "niezrzeszona": "Niezrzeszeni",
        "niezrzeszony": "Niezrzeszeni",
    }
    lower = name.lower().strip()
    for key, val in mapping.items():
        if key in lower:
            return val
    return name


def parse_olsztyn_list(soup: BeautifulSoup, config: CityConfig) -> dict[str, list[str]]:
    """Olsztyn BIP: list of councilors with club info in description."""
    clubs: dict[str, list[str]] = {}
    # Olsztyn has a specific format - needs custom handling
    # Fall back to generic heading parser
    return parse_club_headings(soup, config)


# ---------------------------------------------------------------------------
# Load current data from code
# ---------------------------------------------------------------------------

def load_current_councilors(slug: str) -> dict[str, str]:
    """Load current COUNCILORS dict from scrape script or club_assignments from config."""
    # Try config.json club_assignments first
    config_path = CITIES_DIR / slug / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        ca = cfg.get("club_assignments", {})
        if ca:
            return ca

    # Try to extract COUNCILORS from scrape script
    scripts_dir = CITIES_DIR / slug / "scripts"
    if scripts_dir.exists():
        for py_file in sorted(scripts_dir.glob("scrape_*.py")):
            text = py_file.read_text(encoding="utf-8")
            # Simple regex to find "Name": "Club" patterns in COUNCILORS dict
            m = re.search(r'COUNCILORS\s*[=:]\s*\{', text)
            if m:
                councilors = {}
                # Find the dict body
                start = m.end()
                depth = 1
                i = start
                while i < len(text) and depth > 0:
                    if text[i] == '{':
                        depth += 1
                    elif text[i] == '}':
                        depth -= 1
                    i += 1
                dict_body = text[start:i-1]
                # Extract all "name": "club" pairs
                for match in re.finditer(r'"([^"]+)"\s*:\s*"([^"]+)"', dict_body):
                    name = match.group(1)
                    club = match.group(2)
                    if club and not club.startswith('#') and len(club) < 50:
                        councilors[name] = club
                if councilors:
                    return councilors
    return {}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare(bip_data: dict[str, list[str]], current: dict[str, str]) -> dict:
    """Compare BIP data with current code data."""
    # Build reverse map: name → club from BIP
    bip_map: dict[str, str] = {}
    for club, members in bip_data.items():
        for member in members:
            bip_map[member] = club

    results = {
        "matched": [],
        "club_mismatch": [],
        "missing_in_bip": [],
        "missing_in_code": [],
        "bip_club_counts": {k: len(v) for k, v in bip_data.items()},
        "code_club_counts": {},
    }

    # Count code clubs
    code_clubs: dict[str, int] = {}
    for name, club in current.items():
        code_clubs[club] = code_clubs.get(club, 0) + 1
    results["code_club_counts"] = code_clubs

    # Check each councilor in code
    for name, code_club in current.items():
        # Try to find name in BIP data (case-insensitive, token-based)
        matched_bip = _find_name(name, bip_map)

        if matched_bip is None:
            results["missing_in_bip"].append({"name": name, "code_club": code_club})
        elif bip_map[matched_bip] != code_club:
            results["club_mismatch"].append({
                "name": name,
                "code_club": code_club,
                "bip_club": bip_map[matched_bip],
            })
        else:
            results["matched"].append({"name": name, "club": code_club})

    # Check for councilors in BIP but not in code
    for name in bip_map:
        if _find_name(name, current) is None:
            results["missing_in_code"].append({
                "name": name,
                "bip_club": bip_map[name],
            })

    return results


def _find_name(name: str, lookup: dict[str, str]) -> str | None:
    """Find name in lookup dict using flexible matching."""
    # Exact match
    if name in lookup:
        return name

    # Token-based match (order-independent)
    name_tokens = set(name.lower().split())

    for key in lookup:
        key_tokens = set(key.lower().split())
        # Check if all significant tokens match
        if len(name_tokens & key_tokens) >= max(len(name_tokens), len(key_tokens)) - 1:
            return key

    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(city: str, results: dict, bip_data: dict) -> None:
    """Print a human-readable report."""
    print(f"\n{'='*60}")
    print(f"  {city}")
    print(f"{'='*60}")

    total_code = sum(results["code_club_counts"].values())
    total_bip = sum(results["bip_club_counts"].values())

    print(f"\n  Kod: {total_code} radnych | BIP: {total_bip} radnych")
    print(f"  Zgodnych: {len(results['matched'])}")
    print(f"  Różnice klubowe: {len(results['club_mismatch'])}")
    print(f"  Brak w BIP: {len(results['missing_in_bip'])}")
    print(f"  Brak w kodzie: {len(results['missing_in_code'])}")

    if results["club_mismatch"]:
        print(f"\n  ❌ NIEZGODNE PRZYPISANIA:")
        for m in results["club_mismatch"]:
            print(f"    {m['name']}: kod={m['code_club']} → BIP={m['bip_club']}")

    if results["missing_in_bip"]:
        print(f"\n  ⚠️  W KODZIE, BRAK W BIP:")
        for m in results["missing_in_bip"]:
            print(f"    {m['name']} ({m['code_club']})")

    if results["missing_in_code"]:
        print(f"\n  ⚠️  W BIP, BRAK W KODZIE:")
        for m in results["missing_in_code"]:
            print(f"    {m['name']} ({m['bip_club']})")

    if not results["club_mismatch"] and not results["missing_in_code"]:
        print(f"\n  ✅ WSZYSTKO ZGODNE")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Weryfikacja przypisań radnych do klubów")
    ap.add_argument("--city", help="Tylko jedno miasto (slug)")
    ap.add_argument("--report-only", action="store_true", help="Tylko raport, bez zapisu")
    args = ap.parse_args()

    configs = CITY_CONFIGS
    if args.city:
        configs = [c for c in configs if c.slug == args.city]
        if not configs:
            print(f"Nieznane miasto: {args.city}")
            return 1

    session = requests.Session()
    session.headers.update(HEADERS)

    for cfg in configs:
        print(f"\n--- {cfg.name} ({cfg.slug}) ---")
        print(f"  URL: {cfg.url}")

        try:
            resp = session.get(cfg.url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ❌ BŁĄD: {e}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Parse BIP data
        if cfg.parser == "szczecin_table":
            bip_data = parse_szczecin_table(soup, cfg)
        elif cfg.parser == "olsztyn_list":
            bip_data = parse_olsztyn_list(soup, cfg)
        else:
            bip_data = parse_club_headings(soup, cfg)

        if not bip_data:
            print(f"  ⚠️  Nie znaleziono klubów na stronie")
            continue

        print(f"  Kluby z BIP: { {k: len(v) for k, v in bip_data.items()} }")

        # Load current data
        current = load_current_councilors(cfg.slug)
        if not current:
            print(f"  ⚠️  Nie znaleziono danych w kodzie")
            continue

        print(f"  Kluby z kodu: {dict(sorted(_count_clubs(current).items()))}")

        # Compare
        results = compare(bip_data, current)
        print_report(cfg.name, results, bip_data)

    return 0


def _count_clubs(data: dict[str, str]) -> dict[str, int]:
    clubs: dict[str, int] = {}
    for v in data.values():
        clubs[v] = clubs.get(v, 0) + 1
    return clubs


if __name__ == "__main__":
    raise SystemExit(main())
