#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Lublina.

Źródło: bip.lublin.eu
BIP Lublin to standardowy HTML — nie wymaga JavaScript.
Używa requests + BeautifulSoup do scrapowania, PyMuPDF do PDF.

Struktura BIP:
  1. Lista sesji: https://bip.lublin.eu/rada-miasta-lublin/ix-kadencja/sesje/
  2. Sesja (strona): /rada-miasta-lublin/ix-kadencja/sesje/NAZWA-SESJI/
  3. Wyniki głosowań (PDF): "Imienne wykazy głosowań radnych" - attachment

Użycie:
    pip install requests beautifulsoup4 lxml pymupdf
    python scrape_lublin.py [--output docs/data.json] [--profiles docs/profiles.json]

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_clubs import club_has_line  # noqa: E402
from urllib.parse import parse_qs, urljoin, urlparse

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

try:
    import fitz
except ImportError:
    print("Zainstaluj: pip install pymupdf")
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


BIP_BASE = "https://bip.lublin.eu/"
SESSIONS_URLS = [
    f"{BIP_BASE}rada-miasta-lublin/ix-kadencja/sesje/",
]

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 0.3  # politeness sleep przed HTTP, cache hits pomijają

# Radni Lublina IX kadencja (2024-2029)
# Based on research from BIP Lublin and election results (31 total councillors)
# Sources: bip.lublin.eu, radio.lublin.pl
# Źródło: config.json → club_assignments (jedno źródło prawdy)
def _load_councilors() -> dict[str, str]:
    """Load club assignments from config.json (single source of truth)."""
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            return cfg.get("club_assignments", {}) or {}
        except Exception:
            pass
    return {}

COUNCILORS = _load_councilors()

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


_CACHE_DIR: Path | None = None

# Sesje plenarne starsze niż tyle dni traktujemy jako stabilne. BIP nie
# modyfikuje protokołów/wyników po finalizacji posiedzenia (kilka dni max).
STABLE_AGE_DAYS = 2  # sesje >=2 dni cache, bo vote data zwykle publikowany w 24h


def init_cache(cache_dir: str | None) -> None:
    global _CACHE_DIR
    if cache_dir:
        _CACHE_DIR = Path(cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    else:
        _CACHE_DIR = None


def _cache_path(url: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    import hashlib
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.html"


def _is_session_stable(date_str: str) -> bool:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    age = (datetime.now() - dt).days
    return age >= STABLE_AGE_DAYS


def fetch(url: str, use_cache: bool = True) -> BeautifulSoup:
    """Fetch a page (with disk cache) and return BeautifulSoup.

    use_cache=False wymusza świeży HTTP (np. dla list sesji albo świeżych sesji).
    """
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        return BeautifulSoup(cache_file.read_text(encoding="utf-8"), "lxml")

    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    if cache_file:
        try:
            cache_file.write_text(resp.text, encoding="utf-8")
        except Exception:
            pass
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
# Step 1: Scrape session list
# ---------------------------------------------------------------------------

def _extract_sessions_from_soup(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract session info from a BeautifulSoup page."""
    sessions = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]

        # Match text like "XVII Sesja Rady Miasta Lublin IX kadencji w dniu 5 lutego 2026 r."
        m = re.search(
            r'([IVXLCDM]+)\s+sesja.*?(\d{1,2})\s+(\w+)\s+(\d{4})',
            text,
            re.IGNORECASE
        )
        if not m:
            continue

        number = m.group(1).upper()
        day = int(m.group(2))
        month_name = m.group(3).lower()
        year = int(m.group(4))

        month = MONTHS_PL.get(month_name)
        if not month:
            continue
        date = f"{year}-{month:02d}-{day:02d}"

        if not href.startswith("http"):
            href = urljoin(base_url, href)

        sessions.append({
            "number": number,
            "date": date,
            "url": href,
        })
    return sessions


def _fetch_paginated(base_url: str, stop_before_date: str | None = None) -> list[dict]:
    """Fetch a BIP listing page + all pagination pages, extract sessions.

    Strona 1 (base_url) jest zawsze pobierana świeżo. Strony 2+ są cachowane.

    Paginacja następuje wyłącznie przez link "następna" (nie przez numerowane linki
    w nawigacji — BIP może mieć tysiące numerycznych linków do dokumentów, które
    wyglądają jak numery stron i powodują pobieranie 2000+ zbędnych stron).

    stop_before_date: YYYY-MM-DD — przerywa paginację gdy wszystkie sesje na stronie
    są starsze niż ta data.
    """
    sessions = []
    visited = set()
    current_url = base_url
    is_first_page = True

    while current_url:
        if current_url in visited:
            break
        visited.add(current_url)

        try:
            soup = fetch(current_url, use_cache=not is_first_page)
        except Exception as e:
            print(f"  Nie udało się pobrać {current_url}: {e}")
            break

        page_sessions = _extract_sessions_from_soup(soup, base_url)
        sessions.extend(page_sessions)
        is_first_page = False

        if stop_before_date and page_sessions:
            if all(s["date"] < stop_before_date for s in page_sessions):
                break

        current_url = None
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() in ("następna", "»", ">", "next", "→"):
                candidate = urljoin(base_url, a["href"])
                if candidate not in visited:
                    current_url = candidate
                    break

    return sessions


def scrape_session_list() -> list[dict]:
    """Fetch session list from BIP Lublin."""
    sessions = []
    kadencja_start = KADENCJE["2024-2029"]["start"]

    for url in SESSIONS_URLS:
        print(f"  Próbuję: {url}")
        page_sessions = _fetch_paginated(url, stop_before_date=kadencja_start)
        if page_sessions:
            print(f"    → znaleziono {len(page_sessions)} sesji")
            sessions.extend(page_sessions)
            break  # found sessions — skip remaining fallback URLs

    if not sessions:
        print("  UWAGA: Nie znaleziono sesji na żadnej stronie!")
        return []

    # Deduplicate by (number, date)
    seen = set()
    unique = []
    for s in sessions:
        key = (s["number"], s["date"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    # Filter by kadencja — only sessions from 2024-05-07 onwards
    filtered = [s for s in unique if s["date"] >= kadencja_start]
    print(f"  Znaleziono {len(unique)} sesji ogółem, {len(filtered)} w kadencji 2024-2029")

    if not filtered and unique:
        print(f"  UWAGA: Brak sesji po {kadencja_start}.")
        print(f"  Najnowsza znaleziona: {max(s['date'] for s in unique)}")
        # Return all — user can filter manually with --all-kadencje
        return sorted(unique, key=lambda x: x["date"])

    return sorted(filtered, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape session page → find PDF links
# ---------------------------------------------------------------------------

def scrape_session_pdf_links(session: dict) -> list[dict]:
    """Fetch session page and find vote result PDF attachment links.

    BIP Lublin has "Imienne wykazy głosowań radnych" (named vote lists)
    as PDF attachments.

    Stabilne sesje (date > 7 dni temu) używają cache HTML.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    soup = fetch(session["url"], use_cache=is_stable)
    vote_links = []
    total_attachments = 0

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        # Look for PDF links
        if not href.lower().endswith(".pdf") and "/attachments/download/" not in href:
            continue

        total_attachments += 1

        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        # FILTER: Only keep "Imienne wykazy głosowań" or "Głosowania" links
        text_lower = text.lower()
        is_vote = (
            "imienne wykazy" in text_lower
            or "głosowania" in text_lower
            or "głosowanie" in text_lower
        )

        if is_vote:
            # Extract ID if available
            m = re.search(r'/attachments/download/(\d+)', href)
            att_id = m.group(1) if m else None

            vote_links.append({
                "url": href,
                "text": text,
                "att_id": att_id,
            })

    # Deduplicate by attachment ID or URL
    seen_att = set()
    unique_links = []
    for pl in vote_links:
        key = pl.get("att_id") or pl["url"]
        if key not in seen_att:
            seen_att.add(key)
            unique_links.append(pl)

    print(f"    ({total_attachments} załączników ogółem, {len(unique_links)} wyników głosowań)")
    return unique_links


# ---------------------------------------------------------------------------
# Step 3: Download and parse PDF
# ---------------------------------------------------------------------------

def download_pdf(pdf_url: str, cache_dir: Path) -> Path | None:
    """Download a PDF from URL to cache directory."""
    import hashlib
    # Extract ID from URL for filename; fall back to URL hash
    m = re.search(r'/attachments/download/(\d+)', pdf_url)
    if not m:
        m = re.search(r'/(\d+)(?:\.pdf)?$', pdf_url)
    att_id = m.group(1) if m else hashlib.md5(pdf_url.encode()).hexdigest()[:12]

    filename = f"protocol_{att_id}.pdf"
    path = cache_dir / filename

    if path.exists() and path.stat().st_size > 1000:
        print(f"    Cache hit: {filename}")
        return path

    time.sleep(DELAY)
    print(f"    GET {pdf_url}")
    try:
        resp = _session.get(pdf_url, timeout=60)
        resp.raise_for_status()
        # Verify we got a PDF
        if b"%PDF" not in resp.content[:10]:
            print(f"    UWAGA: Nie PDF — prawdopodobnie strona HTML ({len(resp.content)} bytes)")
            return None
        path.write_bytes(resp.content)
        print(f"    Zapisano: {filename} ({len(resp.content)} bytes)")
        return path
    except Exception as e:
        print(f"    BŁĄD pobierania: {e}")
        return None


def _pdf_parse_cache_path(pdf_path: Path) -> Path | None:
    """Cache path dla sparsowanego output PDF. Klucz = SHA256 zawartości pliku.

    Lokalizacja: {cache_dir_pdf}/parsed/{hash}.json. Jeśli _CACHE_DIR jest
    ustawiony, cache parsed output żyje obok cache HTML w tym samym scratchu.
    """
    if _CACHE_DIR is None:
        return None
    try:
        import hashlib as _h
        h = _h.sha256(pdf_path.read_bytes()).hexdigest()[:24]
    except Exception:
        return None
    parsed_dir = _CACHE_DIR.parent / "parsed_pdf"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    return parsed_dir / f"{h}.json"


def parse_vote_from_pdf(pdf_path: Path) -> list[dict]:
    """Parse a single vote PDF from BIP Lublin.

    Each PDF contains one or more votes in inline format:
      N. Głosowanie w sprawie TOPIC - czas głosowania: DATE, godz. TIME,
         wyniki: ZA: N, PRZECIW: N, WSTRZYMUJĘ SIĘ: N, BRAK GŁOSU: N, NIEOBECNI: N
      Wyniki imienne: Name (VOTE), Name (VOTE), ...

    Cache parsed output: PyMuPDF + regex parsing kosztuje ~50-200ms per PDF,
    przy 500+ PDFs to 25-100s. Hash file → JSON cache na dysku.
    """
    # Cache hit check
    cache_file = _pdf_parse_cache_path(pdf_path)
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 2:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass  # corrupted cache, re-parse

    votes = []
    try:
        doc = fitz.open(str(pdf_path))
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"    BŁĄD parsowania PDF: {e}")
        return votes

    if "Wyniki imienne" not in full_text:
        return votes

    # Normalize whitespace: join lines, collapse multiple spaces
    text = re.sub(r'\n', ' ', full_text)
    text = re.sub(r'\s+', ' ', text)

    # Split into individual vote sections by numbered headings
    # Pattern: "N. Głosowanie w sprawie"
    sections = re.split(r'(?=\d+\.\s+Głosowanie\s+w\s+sprawie\s)', text)

    for section in sections:
        section = section.strip()
        if not section or "Wyniki imienne" not in section:
            continue

        # Extract topic: between "Głosowanie w sprawie" and "- czas głosowania"
        topic_m = re.search(
            r'\d+\.\s+Głosowanie\s+w\s+sprawie\s+(.+?)\s*-\s*czas\s+głosowania',
            section
        )
        topic = topic_m.group(1).strip() if topic_m else "Głosowanie"

        # Extract summary counts from "wyniki:" line
        counts = {
            "za": 0,
            "przeciw": 0,
            "wstrzymal_sie": 0,
            "brak_glosu": 0,
            "nieobecni": 0,
        }

        wyniki_m = re.search(r'wyniki:\s*(.+?)\s*Wyniki imienne', section)
        if wyniki_m:
            summary = wyniki_m.group(1)
            for key, pattern in [
                ("za", r'ZA:\s*(\d+)'),
                ("przeciw", r'PRZECIW:\s*(\d+)'),
                ("wstrzymal_sie", r'WSTRZYMUJĘ SIĘ:\s*(\d+)'),
                ("brak_glosu", r'BRAK GŁOSU:\s*(\d+)'),
                ("nieobecni", r'NIEOBECNI:\s*(\d+)'),
            ]:
                m = re.search(pattern, summary)
                if m:
                    counts[key] = int(m.group(1))

        # Extract named votes from "Wyniki imienne: Name (VOTE), ..."
        named_votes = {
            "za": [],
            "przeciw": [],
            "wstrzymal_sie": [],
            "brak_glosu": [],
            "nieobecni": [],
        }

        imienne_m = re.search(r'Wyniki imienne:\s*(.+)', section)
        if not imienne_m:
            continue

        imienne_text = imienne_m.group(1)
        # Parse "Name (VOTE)" pairs
        pairs = re.findall(r'([^,()]+?)\s*\(([^)]+)\)', imienne_text)

        is_attendance = all(
            v.strip().upper() in ("OBECNY", "NIEOBECNY") for _, v in pairs
        ) if pairs else False

        if is_attendance:
            continue

        for name_raw, vote_raw in pairs:
            name = name_raw.strip()
            # Normalize stray spaces around hyphens (e.g. "Szczygieł- Mitrus")
            name = re.sub(r'\s*-\s*', '-', name)
            # Fix known BIP typos
            if name == "Anna Rytka":
                name = "Anna Ryfka"
            vote = vote_raw.strip().upper()

            if not name:
                continue

            if vote == "ZA":
                named_votes["za"].append(name)
            elif vote == "PRZECIW":
                named_votes["przeciw"].append(name)
            elif "WSTRZYMUJ" in vote or "WSTRZYMAŁ" in vote:
                named_votes["wstrzymal_sie"].append(name)
            elif "NIE GŁOSOWAŁ" in vote or vote in ("BRAK GŁOSU",):
                named_votes["brak_glosu"].append(name)
            elif "NIEOBECN" in vote:
                named_votes["nieobecni"].append(name)

        total_named = sum(len(v) for v in named_votes.values())
        if total_named > 0:
            votes.append({
                "topic": topic[:500],
                "counts": counts,
                "named_votes": named_votes,
            })

    # Cache parsed output: file hash → JSON. Drugi run pomija PyMuPDF + regex.
    if cache_file:
        try:
            cache_file.write_text(json.dumps(votes, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return votes


# ---------------------------------------------------------------------------
# Step 4: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping.

    Club assignments from the COUNCILORS dict take priority over
    whatever is stored in profiles.json (to fix stale '?' values).
    """
    result = {}
    path = Path(profiles_path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for p in data.get("profiles", []):
            name = p["name"]
            kadencje = p.get("kadencje", {})
            if kadencje:
                latest = list(kadencje.values())[-1]
                club = COUNCILORS.get(name, latest.get("club", "?"))
                result[name] = {
                    "name": name,
                    "club": club,
                    "district": latest.get("okręg"),
                }
    else:
        print(f"  UWAGA: Brak {profiles_path} — biorę kluby z COUNCILORS dict")

    # Add any councilors from COUNCILORS dict that are not yet in profiles
    for name, club in COUNCILORS.items():
        if name not in result:
            result[name] = {"name": name, "club": club, "district": None}

    return result


def compute_club_majority(vote: dict, profiles: dict) -> dict[str, str]:
    """For each club, compute the majority position in a given vote."""
    club_votes = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    for cat in ["za", "przeciw", "wstrzymal_sie"]:
        for name in vote["named_votes"].get(cat, []):
            club = profiles.get(name, {}).get("club", "?")
            if club_has_line(club):
                club_votes[club][cat] += 1

    majority = {}
    for club, counts in club_votes.items():
        best = max(counts, key=counts.get)
        majority[club] = best
    return majority


def build_councilors(all_votes: list[dict], sessions: list[dict], profiles: dict) -> list[dict]:
    """Build councilor statistics from vote data."""
    all_names = set()
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    councilors = {}
    for name in sorted(all_names):
        prof = profiles.get(name, {})
        councilors[name] = {
            "name": name,
            "club": prof.get("club", "?"),
            "district": prof.get("district"),
            "votes_za": 0,
            "votes_przeciw": 0,
            "votes_wstrzymal": 0,
            "votes_brak": 0,
            "votes_nieobecny": 0,
            "sessions_present": set(),
            "votes_with_club": 0,
            "votes_against_club": 0,
            "rebellions": [],
        }

    for v in all_votes:
        club_majority = compute_club_majority(v, profiles)

        for name in v["named_votes"].get("za", []):
            if name in councilors:
                councilors[name]["votes_za"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "za", club_majority, v)
        for name in v["named_votes"].get("przeciw", []):
            if name in councilors:
                councilors[name]["votes_przeciw"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "przeciw", club_majority, v)
        for name in v["named_votes"].get("wstrzymal_sie", []):
            if name in councilors:
                councilors[name]["votes_wstrzymal"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "wstrzymal_sie", club_majority, v)
        for name in v["named_votes"].get("brak_glosu", []):
            if name in councilors:
                councilors[name]["votes_brak"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("nieobecni", []):
            if name in councilors:
                councilors[name]["votes_nieobecny"] += 1

    # Only count sessions that have vote data
    sessions_with_votes = set(v["session_date"] for v in all_votes if v.get("session_date"))
    total_sessions = len(sessions_with_votes)
    total_votes = len(all_votes)

    result = []
    for c in councilors.values():
        present_votes = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        frekwencja = (len(c["sessions_present"]) / total_sessions * 100) if total_sessions > 0 else 0
        aktywnosc = (present_votes / total_votes * 100) if total_votes > 0 else 0
        total_club_votes = c["votes_with_club"] + c["votes_against_club"]
        zgodnosc = (c["votes_with_club"] / total_club_votes * 100) if total_club_votes > 0 else 0

        result.append({
            "name": c["name"],
            "club": c["club"],
            "district": c["district"],
            "frekwencja": round(frekwencja, 1),
            "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": round(zgodnosc, 1),
            "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": len(c["rebellions"]),
            "rebellions": c["rebellions"],
            "has_activity_data": False,
            "activity": None,
        })

    return sorted(result, key=lambda x: x["name"])


def _check_rebellion(councilor: dict, vote_cat: str, club_majority: dict, vote: dict):
    """Check if councilor voted differently from their club majority."""
    club = councilor["club"]
    if club == "?" or club not in club_majority:
        return
    majority_cat = club_majority[club]
    if vote_cat == majority_cat:
        councilor["votes_with_club"] += 1
    else:
        councilor["votes_against_club"] += 1
        councilor["rebellions"].append({
            "vote_id": vote["id"],
            "session": vote["session_date"],
            "topic": vote["topic"][:120],
            "their_vote": vote_cat,
            "club_majority": majority_cat,
        })


def compute_similarity(all_votes: list[dict], councilors_list: list[dict]) -> tuple[list, list]:
    """Compute councilor pairs with highest/lowest voting similarity."""
    name_to_club = {c["name"]: c["club"] for c in councilors_list}
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
    votes_by_key = defaultdict(list)
    for v in all_votes:
        key = (v["session_date"], v.get("session_number", ""))
        votes_by_key[key].append(v)

    votes_by_date = defaultdict(list)
    for v in all_votes:
        votes_by_date[v["session_date"]].append(v)

    date_counts = Counter(s["date"] for s in sessions_raw)

    result = []
    for s in sessions_raw:
        date = s["date"]
        number = s.get("number", "")

        if date_counts[date] > 1:
            session_votes = votes_by_key.get((date, number), [])
        else:
            session_votes = votes_by_date.get(date, [])

        attendees = set()
        for v in session_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                attendees.update(v["named_votes"].get(cat, []))

        result.append({
            "date": date,
            "number": number,
            "vote_count": len(session_votes),
            "attendee_count": len(attendees),
            "attendees": sorted(attendees),
            "speakers": [],
        })

    return sorted(result, key=lambda x: (x["date"], x["number"]))


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
# Main scraping pipeline
# ---------------------------------------------------------------------------

def scrape(output_path: str, profiles_path: str):
    """Main scraping pipeline."""
    _t0 = time.time()
    _timings: list[tuple[str, float]] = []

    def _mark(name: str) -> None:
        _timings.append((name, time.time() - _t0))

    init_session()
    _mark("init_session")

    # Step 1: Scrape session list
    print("\n=== Pobieranie listy sesji ===")
    sessions = scrape_session_list()
    _mark("scrape_session_list")
    if not sessions:
        print("BŁĄD: Nie znaleziono sesji!")
        return

    print(f"\nZnaleziono {len(sessions)} sesji:\n")
    for s in sessions:
        print(f"  {s['number']:>3} | {s['date']} | {s['url']}")

    # Step 2: Scrape PDF links for each session
    print(f"\n=== Pobieranie linków do PDF ({len(sessions)} sesji) ===")
    all_pdf_links = []
    for session in sessions:
        print(f"\nSesja {session['number']} ({session['date']}):")
        try:
            pdf_links = scrape_session_pdf_links(session)
            for pl in pdf_links:
                all_pdf_links.append({
                    "session_number": session["number"],
                    "session_date": session["date"],
                    **pl,
                })
        except Exception as e:
            print(f"  BŁĄD: {e}")

    if not all_pdf_links:
        print("\nBŁĘD: Nie znaleziono żadnych plików PDF!")
        return

    print(f"\nZnaleziono {len(all_pdf_links)} plików PDF")

    # Step 3: Download and parse PDFs
    print(f"\n=== Pobieranie i analiza PDF ({len(all_pdf_links)}) ===")
    cache_dir = Path(__file__).parent / ".cache"
    cache_dir.mkdir(exist_ok=True)

    all_votes = []
    vote_id = 0
    for i, pdf_info in enumerate(all_pdf_links):
        print(f"\n[{i+1}/{len(all_pdf_links)}] {pdf_info['text'][:60]}")

        pdf_path = download_pdf(pdf_info["url"], cache_dir)
        if not pdf_path:
            continue

        votes = parse_vote_from_pdf(pdf_path)
        for v in votes:
            vote_id += 1
            v["id"] = f"vote_{vote_id}"
            v["session_number"] = pdf_info["session_number"]
            v["session_date"] = pdf_info["session_date"]
            all_votes.append(v)

        time.sleep(DELAY)

    _mark("download_and_parse_pdfs")

    if not all_votes:
        print("\nBŁĘD: Nie udało się wyodrębnić żadnych głosowań!")
        return

    print(f"\nWyodrębniono {len(all_votes)} głosowań")

    # Step 4: Load profiles and build output
    print(f"\n=== Budowanie struktur ===")
    profiles = load_profiles(profiles_path)

    councilors = build_councilors(all_votes, sessions, profiles)
    sessions_out = build_sessions(sessions, all_votes)
    top_pairs, bottom_pairs = compute_similarity(all_votes, councilors)
    _mark("build_output")

    # Step 5: Build final output
    output = {
        "generated_at": datetime.now().isoformat(),
        "scraper": "scrape_lublin.py",
        "city": "Lublin",
        "kadencje": [
            {
                "id": "2024-2029",
                "label": "IX kadencja (2024–2029)",
                "councilors": councilors,
                "sessions": sessions_out,
                "votes": all_votes,
                "total_sessions": len(sessions_out),
                "total_votes": len(all_votes),
                "total_councilors": len(councilors),
                "similarity_top": top_pairs,
                "similarity_bottom": bottom_pairs,
            }
        ]
    }

    # Step 6: Save output
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    save_split_output(output, output_path)

    size_kb = Path(output_path).stat().st_size / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")

    # Step 7: Merge into profiles
    build_profiles_json(output, profiles_path)

    _mark("save_output")

    print(f"\nGotowe!")
    print(f"  Sesji: {len(sessions_out)}")
    print(f"  Radnych: {len(councilors)}")
    print(f"  Głosowań: {len(all_votes)}")

    # Timing breakdown
    print(f"\n=== Timing ===")
    prev = 0.0
    for name, total in _timings:
        delta = total - prev
        print(f"  {name:>28}: +{delta:7.1f}s  (cumulative {total:.1f}s)")
        prev = total


def main():
    parser = argparse.ArgumentParser(
        description="Scraper danych głosowań Rady Miasta Lublina"
    )
    parser.add_argument(
        "--output", default="docs/data.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/data.json)"
    )
    parser.add_argument(
        "--profiles", default="docs/profiles.json",
        help="Ścieżka do pliku profili (domyślnie: docs/profiles.json)"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML stron BIP. Domyślnie {script_dir}/../.cache/html. "
             "Stabilne sesje (>7 dni temu) używają cache zamiast HTTP. "
             "Ustaw '' aby wyłączyć cache."
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Wyłącz cache HTML (każdy run = świeże HTTP dla wszystkich sesji)."
    )
    args = parser.parse_args()

    # Cache HTML — default w cities/lublin/.cache/html/ (w gitignore, persistent)
    if args.no_cache:
        cache_dir = None
    elif args.cache_dir is not None:
        cache_dir = args.cache_dir or None
    else:
        cache_dir = str(Path(__file__).resolve().parent.parent / ".cache" / "html")
    init_cache(cache_dir)
    if cache_dir:
        print(f"Cache HTML: {cache_dir}")

    scrape(args.output, args.profiles)


if __name__ == "__main__":
    main()
