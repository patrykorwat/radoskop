#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Bytom.

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny

Źródło: bytom.esesja.pl
eSesja to platforma do głosowań elektronicznych.

Struktura eSesja:
  1. Archiwum glosowan: https://bytom.esesja.pl/glosowania
     Linki do sesji w formacie /listaglosowan/{UUID}
  2. Lista glosowan w sesji: /listaglosowan/{UUID}
     Linki do pojedynczych glosowan /glosowanie/{ID}/{HASH}
  3. Wyniki glosowania: /glosowanie/{ID}/{HASH}
     div.wim > h3 (kategoria: ZA/PRZECIW/...) > div.osobaa (nazwisko)

Użycie:
    pip install requests beautifulsoup4 lxml
    python scrape_bialystok.py [--output docs/data.json] [--profiles docs/profiles.json]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4 lxml")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)

def compact_named_votes(output):
    """Convert named_votes from string arrays to indexed format for smaller JSON."""
    for kad in output.get("kadencje", []):
        names = set()
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat_names in nv.values():
                for n in cat_names:
                    if isinstance(n, str):
                        names.add(n)
        if not names:
            continue
        index = sorted(names, key=lambda n: n.split()[-1] + " " + n)
        name_to_idx = {n: i for i, n in enumerate(index)}
        kad["councilor_index"] = index
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat in nv:
                nv[cat] = sorted(name_to_idx[n] for n in nv[cat] if isinstance(n, str) and n in name_to_idx)
    return output



def save_split_output(output, out_path):
    """Save output as split files: data.json (index) + kadencja-{id}.json per kadencja."""
    import json as _json
    from pathlib import Path as _Path
    compact_named_votes(output)
    out_path = _Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        kad_path = out_path.parent / f"kadencja-{kid}.json"
        with open(kad_path, "w", encoding="utf-8") as f:
            _json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": output.get("generated", ""),
        "default_kadencja": output.get("default_kadencja", ""),
        "kadencje": stubs,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESESJA_BASE = "https://bytom.esesja.pl"
SESSIONS_URL = f"{ESESJA_BASE}/glosowania"

DELAY = 1.0

KADENCJE = {
    "2024-2029": {
        "label": "IX kadencja (2024–2029)",
        "start": "2024-05-07",
    },
}

# Councillor names and club assignments for IX kadencja (2024-2029)
# Source: BIP Bytom, portal samorządowy
COUNCILORS: dict[str, str] = {}

# Build flexible name lookup: eSesja uses "Lastname Firstname [MiddleName]"
# while COUNCILORS uses "Firstname Lastname".
def _build_name_lookup(councilors: dict[str, str]) -> dict[str, str]:
    """Build a dict that maps multiple name forms to club."""
    lookup = {}
    for name, club in councilors.items():
        lookup[name] = club
        parts = name.split()
        if len(parts) >= 2:
            # "Firstname Lastname" -> "Lastname Firstname"
            lookup[f"{parts[-1]} {' '.join(parts[:-1])}"] = club
            # Also just "Lastname Firstname" (no middle names)
            lookup[f"{parts[-1]} {parts[0]}"] = club
    return lookup

_CLUB_LOOKUP = _build_name_lookup(COUNCILORS)


def resolve_club(name: str) -> str:
    """Resolve a councillor name (any format) to their club."""
    if name in _CLUB_LOOKUP:
        return _CLUB_LOOKUP[name]
    # Try matching by last name (first word in eSesja format)
    parts = name.split()
    if parts:
        last = parts[0]
        for key, club in _CLUB_LOOKUP.items():
            if key.split()[0] == last or key.split()[-1] == last:
                return club
    return ""


# Reusable HTTP session
_session = None


def init_session():
    """Create a requests session with proper headers."""
    global _session
    _session = requests.Session()
    _session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9",
    })


def fetch(url: str) -> BeautifulSoup:
    """Fetch a page and return BeautifulSoup."""
    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    # eSesja pages declare windows-1250 in meta charset but the HTTP header
    # omits charset, so requests falls back to ISO-8859-1 which mangles
    # Polish characters (ł→³, ą→¹, ę→ê, etc.)
    if "esesja" in url:
        resp.encoding = "windows-1250"
    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# Polish month name → number mapping
# ---------------------------------------------------------------------------

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
    "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5,
    "czerwiec": 6, "lipiec": 7, "sierpień": 8, "wrzesień": 9,
    "październik": 10, "listopad": 11, "grudzień": 12, "styczeń": 1,
}


def parse_polish_date(text: str) -> str | None:
    """Parse '25 Listopada 2024 r.' or '25 Listopada 2024' → '2024-11-25'."""
    text = text.strip().rstrip(".")
    # Remove trailing 'r' or 'r.'
    text = re.sub(r'\s*r\.?$', '', text)
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS_PL.get(month_name)
    if not month:
        return None
    return f"{year}-{month:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# Step 1: Scrape session list from eSesja
# ---------------------------------------------------------------------------

def scrape_session_list() -> list[dict]:
    """Fetch session list from eSesja /glosowania page.

    eSesja lists sessions as <a href="/listaglosowan/{UUID}"> with text like
    "sesja Rady Miasta Bialystok w dniu 23 lutego 2026, godz. 09:00".
    The page is paginated: /glosowania/, /glosowania/2, /glosowania/3, ...
    """
    sessions = []
    page = 1

    while True:
        url = SESSIONS_URL if page == 1 else f"{SESSIONS_URL}/{page}"
        try:
            soup = fetch(url)
        except Exception as e:
            print(f"  Nie udalo sie pobrac {url}: {e}")
            break

        found_on_page = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/listaglosowan/" not in href:
                continue

            text = a.get_text(strip=True)

            # Extract date from "w dniu 23 lutego 2026"
            m = re.search(r'w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})', text)
            if not m:
                continue

            day = int(m.group(1))
            month_name = m.group(2).lower()
            year = int(m.group(3))
            month = MONTHS_PL.get(month_name)
            if not month:
                continue

            date_str = f"{year}-{month:02d}-{day:02d}"
            full_url = href if href.startswith("http") else ESESJA_BASE + href

            # Extract session number from text if present (e.g. "nr XXVI")
            nr_match = re.search(r'nr\s+([IVXLCDM]+)', text)
            session_number = nr_match.group(1) if nr_match else ""

            sessions.append({
                "id": full_url.split("/")[-1],
                "date": date_str,
                "number": session_number,
                "url": full_url,
                "title": text,
            })
            found_on_page += 1

        if found_on_page == 0:
            break

        # Check for next page link
        next_link = soup.find("a", href=re.compile(rf'/glosowania/{page + 1}\b'))
        if not next_link:
            break
        page += 1

    if not sessions:
        print("  UWAGA: Nie znaleziono sesji!")
        return []

    # Deduplicate by URL
    seen = set()
    unique = []
    for s in sessions:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique.append(s)

    # Filter by kadencja
    kadencja_start = KADENCJE["2024-2029"]["start"]
    filtered = [s for s in unique if s["date"] >= kadencja_start]

    print(f"  Znaleziono {len(unique)} sesji ogolnie, {len(filtered)} w kadencji 2024-2029")

    return sorted(filtered, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape votes from session page
# ---------------------------------------------------------------------------

def scrape_votes_from_session(session: dict) -> list[dict]:
    """Fetch eSesja vote list page (/listaglosowan/UUID) and then each
    individual vote detail page (/glosowanie/ID/HASH).
    """
    votes = []

    try:
        soup = fetch(session["url"])
    except Exception as e:
        print(f"    Blad pobierania sesji: {e}")
        return votes

    # Collect unique /glosowanie/ID/HASH links
    seen_urls = set()
    vote_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/glosowanie/" not in href or "/listaglosowan/" in href:
            continue
        url = href if href.startswith("http") else ESESJA_BASE + href
        if url in seen_urls:
            continue
        seen_urls.add(url)
        vote_links.append(url)

    print(f"    Znaleziono {len(vote_links)} linkow do glosowan")

    for idx, url in enumerate(vote_links):
        vote = _scrape_single_vote(url, session, idx)
        if vote:
            votes.append(vote)
        time.sleep(DELAY * 0.5)

    print(f"    Wyodrebniono {len(votes)} glosowan z imiennymi wynikami")
    return votes


def _scrape_single_vote(url: str, session: dict, vote_idx: int) -> dict | None:
    """Fetch a single eSesja vote page and parse named results.

    eSesja HTML structure:
      <div class='wim'><h3>ZA<span class='za'> (30)</span></h3>
        <div class='osobaa'>Surname FirstName</div>
        ...
      </div>
    """
    try:
        soup = fetch(url)
    except Exception as e:
        print(f"      Blad pobierania {url}: {e}")
        return None

    # Extract topic from h1
    topic = ""
    h1 = soup.find("h1")
    if h1:
        topic = h1.get_text(strip=True)[:500]
    # Clean eSesja prefixes
    topic = re.sub(r'^Wyniki g\u0142osowania jawnego w sprawie:\s*', '', topic).strip()
    topic = re.sub(r'^Wyniki g\u0142osowania w sprawie:?\s*', '', topic).strip()
    topic = re.sub(r'^G\u0142osowanie\s+w\s+sprawie\s+', '', topic).strip()
    if not topic:
        topic = f"Glosowanie {vote_idx + 1}"

    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    counts = {
        "za": 0,
        "przeciw": 0,
        "wstrzymal_sie": 0,
        "brak_glosu": 0,
        "nieobecni": 0,
    }

    # Parse named votes from div.wim sections.
    # Each div.wim has an h3 header (ZA/PRZECIW/...) and div.osobaa children.
    category_map = {
        "za": "za",
        "przeciw": "przeciw",
        "wstrzymuj": "wstrzymal_sie",
        "brak g": "brak_glosu",
        "nieobecn": "nieobecni",
    }

    for wim in soup.find_all("div", class_="wim"):
        h3 = wim.find("h3")
        if not h3:
            continue
        h3_text = h3.get_text(strip=True).upper()
        cat_key = None
        for prefix, key in category_map.items():
            if h3_text.upper().startswith(prefix.upper()):
                cat_key = key
                break
        if not cat_key:
            continue
        for osoba in wim.find_all("div", class_="osobaa"):
            name = osoba.get_text(strip=True)
            if name and len(name) > 2:
                named_votes[cat_key].append(name)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        return None

    for cat in named_votes:
        counts[cat] = len(named_votes[cat])

    vote_id = f"{session['date']}_{vote_idx:03d}_000"

    return {
        "id": vote_id,
        "source_url": url,
        "session_date": session["date"],
        "session_number": session.get("number", ""),
        "topic": topic[:500],
        "druk": None,
        "resolution": None,
        "counts": counts,
        "named_votes": named_votes,
    }


# ---------------------------------------------------------------------------
# Step 3: Build councillor profiles
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load existing profiles.json if available."""
    path = Path(profiles_path)
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return {p["name"]: p for p in data.get("profiles", [])}
    except Exception:
        return {}


def build_councilors(all_votes: list[dict], sessions: list[dict], existing_profiles: dict) -> list[dict]:
    """Build councillor profiles with voting statistics."""
    stats = defaultdict(lambda: {
        "name": "",
        "club": "",
        "district": None,
        "votes_za": 0,
        "votes_przeciw": 0,
        "votes_wstrzymal": 0,
        "votes_brak": 0,
        "votes_nieobecny": 0,
        "votes_total": 0,
        "frekwencja": 0,
        "aktywnosc": 0,
        "zgodnosc_z_klubem": 0,
        "rebellion_count": 0,
        "rebellions": [],
        "has_voting_data": True,
        "has_activity_data": False,
    })

    # Collect vote counts
    for vote in all_votes:
        for cat, names in vote["named_votes"].items():
            for name in names:
                stats[name]["name"] = name
                stats[name]["club"] = resolve_club(name)
                stats[name]["votes_total"] += 1

                if cat == "za":
                    stats[name]["votes_za"] += 1
                elif cat == "przeciw":
                    stats[name]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    stats[name]["votes_wstrzymal"] += 1
                elif cat == "brak_glosu":
                    stats[name]["votes_brak"] += 1

    # Calculate percentages
    for name, s in stats.items():
        if s["votes_total"] > 0:
            s["frekwencja"] = round((s["votes_total"] - s["votes_brak"]) / s["votes_total"] * 100, 1)
            s["aktywnosc"] = round((s["votes_za"] + s["votes_przeciw"] + s["votes_wstrzymal"]) / s["votes_total"] * 100, 1)

    # Merge with existing profiles
    result = []
    for name, s in sorted(stats.items()):
        if name in existing_profiles:
            s.update({k: v for k, v in existing_profiles[name].items() if k not in s or not s[k]})
        result.append(s)

    return result


def compute_similarity(all_votes: list[dict], councilors: list[dict]) -> tuple:
    """Compute voting similarity between councillors."""
    name_to_club = {c["name"]: c.get("club", "?") for c in councilors}

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat

    names = sorted(vectors.keys())
    pairs = []
    for a, b in combinations(names, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        score = round(same / len(common) * 100, 1)
        pairs.append({
            "a": a,
            "b": b,
            "club_a": name_to_club.get(a, "?"),
            "club_b": name_to_club.get(b, "?"),
            "score": score,
            "common_votes": len(common),
        })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    top = pairs[:20]
    bottom = pairs[-20:][::-1]
    return top, bottom


def build_sessions(sessions_raw: list[dict], all_votes: list[dict]) -> list[dict]:
    """Build session data with attendee info."""
    votes_by_date = defaultdict(list)
    for v in all_votes:
        votes_by_date[v["session_date"]].append(v)

    result = []
    for s in sessions_raw:
        date = s["date"]
        session_votes = votes_by_date.get(date, [])

        attendees = set()
        for v in session_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                attendees.update(v["named_votes"].get(cat, []))

        result.append({
            "date": date,
            "number": s.get("number", ""),
            "vote_count": len(session_votes),
            "attendee_count": len(attendees),
            "attendees": sorted(attendees),
            "speakers": [],
        })

    return sorted(result, key=lambda x: x["date"])


def make_slug(name: str) -> str:
    """Create URL-safe slug from Polish name."""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    slug = name.lower()
    for pl, ascii_c in replacements.items():
        slug = slug.replace(pl, ascii_c)
    slug = slug.replace(' ', '-').replace("'", "")
    return slug


def build_profiles_json(output: dict, profiles_path: str):
    """Build profiles.json from data.json councilors (kadencje format with slugs)."""
    profiles = []
    for kad in output["kadencje"]:
        kid = kad["id"]
        for c in kad["councilors"]:
            entry = {
                "club": c.get("club", "?"),
                "frekwencja": c.get("frekwencja", 0),
                "aktywnosc": c.get("aktywnosc", 0),
                "zgodnosc_z_klubem": c.get("zgodnosc_z_klubem", 0),
                "votes_za": c.get("votes_za", 0),
                "votes_przeciw": c.get("votes_przeciw", 0),
                "votes_wstrzymal": c.get("votes_wstrzymal", 0),
                "votes_brak": c.get("votes_brak", 0),
                "votes_nieobecny": c.get("votes_nieobecny", 0),
                "votes_total": c.get("votes_total", 0),
                "rebellion_count": c.get("rebellion_count", 0),
                "rebellions": c.get("rebellions", []),
                "has_voting_data": True,
                "has_activity_data": c.get("has_activity_data", False),
                "roles": [],
                "notes": "",
                "former": False,
                "mid_term": False,
            }
            if c.get("activity"):
                entry["activity"] = c["activity"]
            profiles.append({
                "name": c["name"],
                "slug": make_slug(c["name"]),
                "kadencje": {kid: entry},
            })

    path = Path(profiles_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)
    print(f"  Zapisano profiles.json: {len(profiles)} profili")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Bytom (eSesja)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")

    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    init_session()

    print("\n=== Radoskop Bytom — scraper ===\n")

    print("[1/4] Pobieranie listy sesji...")
    sessions = scrape_session_list()

    if not sessions:
        print("BŁĄD: Nie znaleziono sesji.")
        sys.exit(1)

    if args.max_sessions > 0:
        sessions = sessions[:args.max_sessions]

    print(f"  Znaleziono {len(sessions)} sesji\n")

    if args.dry_run:
        print("Dry-run: Zatrzymuję się tutaj.")
        return

    print("[2/4] Pobieranie głosowań z sesji...")
    all_votes = []
    for i, session in enumerate(sessions):
        print(f"  [{i+1}/{len(sessions)}] Sesja {session['id']} ({session['date']})")
        votes = scrape_votes_from_session(session)
        all_votes.extend(votes)

    print(f"  Pobrano {len(all_votes)} głosowań\n")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        print("Tworze pusty plik wyjściowy...")
        all_votes = []

    print("[3/4] Budowanie danych...")

    profiles = load_profiles(args.profiles)
    councilors = build_councilors(all_votes, sessions, profiles)
    sessions_data = build_sessions(sessions, all_votes)
    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"  Kluby: {dict(club_counts)}\n")

    kid = "2024-2029"
    kad_output = {
        "id": kid,
        "label": KADENCJE[kid]["label"],
        "clubs": {club: count for club, count in sorted(club_counts.items())},
        "sessions": sessions_data,
        "total_sessions": len(sessions_data),
        "total_votes": len(all_votes),
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": all_votes,
        "similarity_top": sim_top,
        "similarity_bottom": sim_bottom,
    }

    output = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [kad_output],
    }

    print("[4/4] Zapisywanie danych...")

    out_path = Path(args.output)
    save_split_output(output, out_path)

    print(f"Gotowe! Zapisano do {out_path}")
    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych\n")

    build_profiles_json(output, args.profiles)


if __name__ == "__main__":
    main()
