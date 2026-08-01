#!/usr/bin/env python3
"""
Scraper Gemeinderat Zürich z PARIS API (Parlamentsinformationssystem).

ŹRÓDŁO DANYCH
=============
Zürich publikuje dane o głosowaniach imiennych przez REST API w formacie XML.
Endpoint: https://www.gemeinderat-zuerich.ch/api/
Indeks: abstimmung (głosowania)
Każde głosowanie zawiera listę Stimmabgabe z:
  - Name, Vorname (imię i nazwisko radnego)
  - Partei (partia)
  - Fraktion (frakcja)
  - Abstimmungsverhalten (głos: Ja/Nein/Enthaltung/Abwesend)

API używa CQL (Contextual Query Language) do wyszukiwania.
Dokumentacja: https://data.stadt-zuerich.ch/dataset/parlamentsdienste_paris_api

PIPELINE
========
1. GET listy głosowań z /abstimmung/searchdetails (paginacja po 1000).
2. Per głosowanie wyciągnij: SitzungDatum, TraktandumTitel, Schlussresultat,
   oraz imienną listę Stimmabgabe.
3. Zbuduj docs/kadencja-{id}.json + docs/profiles.json.

Mapowanie głosu -> kategoria Radoskop:
   Ja           -> za
   Nein         -> przeciw
   Enthaltung   -> wstrzymal_sie
   Abwesend     -> nieobecni

Użycie:
    python3 scrape_zurich.py
    python3 scrape_zurich.py --max-votes 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
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
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = "Mozilla/5.0 Radoskop/1.0 (+https://radoskop.eu)"
TIMEOUT = 90
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.5

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Abstimmungsverhalten -> kategoria Radoskop
VOTE_MAP = {
    "Ja": "za",
    "Nein": "przeciw",
    "Enthaltung": "wstrzymal_sie",
    "Abwesend": "nieobecni",
}

NS = "http://www.cmiag.ch/cdws"


def http_get(url: str, cache_path: Path | None = None) -> str:
    """GET z retry i opcjonalnym cache."""
    if cache_path and cache_path.is_file():
        return cache_path.read_text(encoding="utf-8")

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read().decode("utf-8")
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(data, encoding="utf-8")
            return data
        except (HTTPError, URLError, OSError) as e:
            print(f"  HTTP error (attempt {attempt}/{RETRY_COUNT}): {e}", file=sys.stderr)
            if attempt < RETRY_COUNT:
                time.sleep(SLEEP_BETWEEN_CALLS * attempt)
            else:
                raise
    return ""


def fetch_all_votes(api_base: str, cache_dir: Path | None, max_votes: int = 0) -> list[dict[str, Any]]:
    """Fetch all votes from the API with pagination."""
    all_votes = []
    page_size = 1000
    start = 1

    while True:
        if max_votes and len(all_votes) >= max_votes:
            break

        url = (
            f"{api_base}/abstimmung/searchdetails"
            f"?q=seq>0&l=de-CH&s={start}&m={page_size}"
        )
        cache_path = None
        if cache_dir:
            cache_path = cache_dir / f"abstimmung_s{start}_m{page_size}.xml"

        print(f"[zurich] GET start={start}, m={page_size}", file=sys.stderr)
        xml_data = http_get(url, cache_path)

        root = ET.fromstring(xml_data)
        num_hits = int(root.attrib.get("numHits", "0"))

        page_votes = 0
        for hit in root:
            if hit.tag.endswith("}Hit") or hit.tag == "Hit":
                vote = parse_vote_hit(hit)
                if vote:
                    all_votes.append(vote)
                    page_votes += 1
                    if max_votes and len(all_votes) >= max_votes:
                        break

        print(
            f"[zurich] start={start}: {page_votes} votes (total {len(all_votes)}/{num_hits})",
            file=sys.stderr,
        )

        if page_votes < page_size or len(all_votes) >= num_hits:
            break

        start += page_size
        time.sleep(SLEEP_BETWEEN_CALLS)

    return all_votes


def parse_vote_hit(hit: ET.Element) -> dict[str, Any] | None:
    """Parse a single vote hit from the API response."""
    # Find the Abstimmung element (might be namespaced or not)
    abstimmung = None
    for child in hit:
        tag = child.tag
        if tag.endswith("}Abstimmung") or tag == "Abstimmung":
            abstimmung = child
            break

    if abstimmung is None:
        return None

    vote: dict[str, Any] = {
        "guid": hit.attrib.get("Guid", ""),
        "stimmabgaben": [],
    }

    for child in abstimmung:
        tag = child.tag.split("}")[-1]  # Strip namespace
        text = (child.text or "").strip()

        if tag == "Stimmabgaben":
            for sa in child:
                vote["stimmabgaben"].append(parse_stimmabgabe(sa))
        elif tag in (
            "SitzungGuid", "SitzungTitel", "SitzungDatum",
            "TraktandumGuid", "TraktandumNr", "TraktandumTitel",
            "GeschaeftGuid", "GeschaeftTitel", "GeschaeftGrNr",
            "GeschaeftRatsgeschaeftsart",
            "Abstimmungstitel", "Nummer", "Abstimmungstyp",
            "Schlussresultat",
        ):
            vote[tag] = text
        elif tag in (
            "Anzahl_Ja", "Anzahl_Nein", "Anzahl_Enthaltung",
            "Anzahl_Abwesend",
        ):
            # Handle xsi:nil
            nil = child.attrib.get(
                f"{{http://www.w3.org/2001/XMLSchema-instance}}nil", "false"
            )
            vote[tag] = None if nil == "true" else (int(text) if text else 0)

    return vote


def parse_stimmabgabe(sa: ET.Element) -> dict[str, Any]:
    """Parse a single Stimmabgabe element."""
    result: dict[str, Any] = {}
    for child in sa:
        tag = child.tag.split("}")[-1]
        text = (child.text or "").strip()

        if tag == "Alter":
            nil = child.attrib.get(
                f"{{http://www.w3.org/2001/XMLSchema-instance}}nil", "false"
            )
            result[tag] = None if nil == "true" else (int(text) if text else None)
        elif tag in ("KontaktGuid", "Name", "Vorname", "Partei", "Fraktion",
                     "Spezialfunktion", "Geschlecht", "Abstimmungsverhalten"):
            result[tag] = text

    return result


def build_kadencja(
    all_votes: list[dict[str, Any]],
    config: dict[str, Any],
    kid: str,
) -> dict[str, Any]:
    """Build kadencja data from all votes."""
    sessions: dict[str, dict[str, Any]] = {}
    votes_out: list[dict[str, Any]] = []
    councilor_index: dict[str, dict[str, str]] = {}
    club_by_name: dict[str, str] = {}
    vote_id_counter = 0

    for vote in all_votes:
        session_date = vote.get("SitzungDatum", "")
        session_title = vote.get("SitzungTitel", "")
        session_key = f"{session_date}_{session_title}"

        if session_key not in sessions:
            sessions[session_key] = {
                "date": session_date[:10] if session_date else "",
                "title": session_title,
                "source_url": "",
            }

        vote_id_counter += 1
        vote_id = str(vote_id_counter)

        # Map individual votes
        named_votes: dict[str, str] = {}
        for sa in vote.get("stimmabgaben", []):
            name = f"{sa.get('Vorname', '')} {sa.get('Name', '')}".strip()
            party = sa.get("Partei", "")
            fraktion = sa.get("Fraktion", "")
            vote_val = sa.get("Abstimmungsverhalten", "")

            # Build councilor key
            guid = sa.get("KontaktGuid", "")
            if guid:
                councilor_index[guid] = {
                    "name": name,
                    "party": party,
                    "fraktion": fraktion,
                }
                club_by_name[name] = fraktion or party

            # Map vote
            category = VOTE_MAP.get(vote_val, "brak_glosu")
            named_votes[guid] = category

        # Count categories
        counts = {c: 0 for c in CATEGORIES}
        for cat in named_votes.values():
            counts[cat] = counts.get(cat, 0) + 1

        votes_out.append({
            "id": vote_id,
            "session_date": session_date[:10] if session_date else "",
            "session_title": session_title,
            "topic": vote.get("TraktandumTitel", "")[:200],
            "title": vote.get("Abstimmungstitel", ""),
            "number": vote.get("Nummer", ""),
            "result_native": vote.get("Schlussresultat", ""),
            "counts": counts,
            "named_votes": named_votes,
        })

    return {
        "sessions": list(sessions.values()),
        "votes": votes_out,
        "councilor_index": councilor_index,
        "club_by_name": club_by_name,
    }


def build_profiles(
    councilor_index: dict[str, dict[str, str]],
    club_by_name: dict[str, str],
) -> list[dict[str, Any]]:
    """Build profiles.json from councilor index."""
    profiles = []
    seen = set()
    for guid, info in councilor_index.items():
        name = info["name"]
        if name in seen:
            continue
        seen.add(name)
        profiles.append({
            "name": name,
            "club_slug": info.get("fraktion", "") or info.get("party", ""),
            "role": "",
            "photo_url": "",
        })
    return profiles


def build_club_assignments(
    councilor_index: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Build club_assignments.json."""
    assignments: dict[str, str] = {}
    for guid, info in councilor_index.items():
        name = info["name"]
        club = info.get("fraktion", "") or info.get("party", "")
        if name and club:
            assignments[name] = club
    return assignments


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper Gemeinderat Zürich")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        type=Path,
        help="Ścieżka do config.json",
    )
    parser.add_argument(
        "--docs",
        default=DEFAULT_DOCS,
        type=Path,
        help="Katalog wyjściowy docs/",
    )
    parser.add_argument(
        "--cache",
        default=DEFAULT_CACHE,
        type=Path,
        help="Katalog cache",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Użyj tylko cache, nie ściągaj z API",
    )
    parser.add_argument(
        "--max-votes",
        type=int,
        default=0,
        help="Limit liczby głosowań (0 = bez limitu)",
    )
    parser.add_argument(
        "--kadencja-id",
        help="ID kadencji do wygenerowania (domyślnie wszystkie z configu)",
    )
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    cache = None if args.skip_fetch else args.cache
    api_base = config.get("api_base", "https://www.gemeinderat-zuerich.ch/api")
    args.docs.mkdir(parents=True, exist_ok=True)

    # Fetch all votes
    all_votes = fetch_all_votes(api_base, cache, args.max_votes or 0)

    if args.max_votes and len(all_votes) > args.max_votes:
        all_votes = all_votes[: args.max_votes]
        print(f"[zurich] LIMIT: {len(all_votes)} głosowań", file=sys.stderr)

    print(f"[zurich] łącznie {len(all_votes)} głosowań imiennych", file=sys.stderr)

    # Clean old kadencja files
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            old.unlink()

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kadencje_to_generate = (
        [args.kadencja_id] if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    all_clubs: dict[str, str] = {}
    for kid in kadencje_to_generate:
        kdef = config["kadencje"][kid]
        built = build_kadencja(all_votes, config, kid)

        if not built["votes"]:
            print(f"[zurich] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue

        out = {
            "id": kid,
            "label": kdef.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": built["sessions"],
            "votes": built["votes"],
            "councilor_index": built["councilor_index"],
        }
        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[zurich] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )
        all_clubs.update(built["club_by_name"])

    # club_assignments.json
    club_path = args.docs / "club_assignments.json"
    with open(club_path, "w", encoding="utf-8") as f:
        json.dump(all_clubs, f, ensure_ascii=False, indent=2)
    print(
        f"[zurich] wrote {club_path.name}: {len(all_clubs)} radnych",
        file=sys.stderr,
    )

    # profiles.json
    councilor_index = {}
    for kid in kadencje_to_generate:
        kpath = args.docs / f"kadencja-{kid}.json"
        if kpath.is_file():
            with open(kpath, "r", encoding="utf-8") as f:
                kdata = json.load(f)
            councilor_index.update(kdata.get("councilor_index", {}))

    profiles = build_profiles(councilor_index, all_clubs)
    profiles_path = args.docs / "profiles.json"
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(
        f"[zurich] wrote {profiles_path.name}: {len(profiles)} radnych",
        file=sys.stderr,
    )

    print("[zurich] OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
