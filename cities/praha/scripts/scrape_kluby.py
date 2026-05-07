#!/usr/bin/env python3
"""
Pobieram skład klubów politycznych w Pražském zastupitelstvu i wpisuję
mapowanie radny → klub do config.json (pole club_assignments).

API:
    GET /o/prg/clubs/period/{periodId}
        Lista klubów: id, name, memberCount, established, cancelled.

    GET /o/prg/clubs/{clubId}/members/{periodId}
        Lista członków klubu: id, fullName, email, klub.

Klucze klubów w config.json (SPOLU, ANO, Pirati, PrahaSobe, STAN, SPD)
mapujemy po praha_club_id z config.clubs.

Użycie:
    python3 scrape_kluby.py
    python3 scrape_kluby.py --period-id -33394
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)


def http_get_json(url: str, timeout: int = 30):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_name(full: str) -> str:
    """Identyczne z scrape_glosowania.normalize_name; obie endpointy
    /clubs/{id}/members/{pid} oraz /voting/detail/{id}/votes zwracają
    "Tytuł Imię Nazwisko" → po stripie tytułów → "Imię Nazwisko".
    """
    s = full.strip().rstrip(",")
    titles = {
        "Mgr", "Ing", "MUDr", "MVDr", "PhDr", "JUDr", "RNDr",
        "Bc", "BcA", "MgA", "doc", "prof", "PaedDr", "ThDr",
        "Dr", "Ph", "PhD", "CSc", "MBA", "MSc", "DiS", "DrSc",
        "arch", "et", "M", "A", "LL", "D", "h",
    }
    out = []
    for t in s.split():
        clean = t.rstrip(",").rstrip(".")
        clean_no_dots = clean.replace(".", "")
        if clean_no_dots in titles or clean.lower() in (x.lower() for x in titles):
            continue
        if all(p in titles or p == "" for p in clean.split(".")):
            continue
        out.append(t.rstrip(","))
    return " ".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--period-id", type=int, default=None,
                        help="Default: praha_period_id z config.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Wypisz mapping ale nie pisz do config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    api_base = config.get("praha_api_base", "https://praha.eu")
    period_id = args.period_id or config.get("praha_period_id")
    if not period_id:
        print("[kluby] brak period_id", file=sys.stderr)
        return 1

    # Mapowanie praha_club_id -> klucz w config.clubs
    club_id_to_key: dict[int, str] = {}
    for key, info in config.get("clubs", {}).items():
        cid = info.get("praha_club_id")
        if cid is not None:
            club_id_to_key[int(cid)] = key

    if not club_id_to_key:
        print("[kluby] config.clubs nie ma żadnego praha_club_id", file=sys.stderr)
        return 1

    print(f"[kluby] pobieram listę klubów (period={period_id})", file=sys.stderr)
    clubs = http_get_json(f"{api_base}/o/prg/clubs/period/{period_id}")
    print(f"        → {len(clubs)} klubów na API", file=sys.stderr)

    assignments: dict[str, str] = {}
    for club in clubs:
        cid = club.get("id")
        cname = club.get("name", "")
        key = club_id_to_key.get(cid)
        if not key:
            print(f"  WARN: klub {cid} ({cname}) nie znaleziony w config.clubs", file=sys.stderr)
            continue
        members = http_get_json(f"{api_base}/o/prg/clubs/{cid}/members/{period_id}")
        print(f"  {cname:30s} → {key:10s} ({len(members)} czł.)", file=sys.stderr)
        for m in members:
            full = m.get("fullName", "")
            canonical = normalize_name(full)
            if canonical:
                assignments[canonical] = key

    if args.dry_run:
        print("\n[kluby] dry-run, mapping:")
        for name, key in sorted(assignments.items()):
            print(f"  {name} → {key}")
        return 0

    config["club_assignments"] = assignments
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n[kluby] zapisano {len(assignments)} mapowań do {config_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
