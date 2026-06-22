#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Krakowa.

Źródło: www.bip.krakow.pl
BIP Kraków to standardowy HTML — nie wymaga JavaScript.
Używa requests + BeautifulSoup.

Struktura BIP:
  1. Lista sesji: ?bip_id=1&mmi=26527
  2. Sesja (posiedzenie): ?dok_id=X&info=posiedzenie&SSJ_ID=Y&PSD_ID=Z
  3. Głosowanie (punkt): ?sub_dok_id=X&info=punkt&PSS_ID=W

Krok 1: Pobierz listę sesji IX kadencji
Krok 2: Dla każdej sesji — pobierz stronę posiedzenia i znajdź linki do głosowań
Krok 3: Dla każdego głosowania — sparsuj wyniki imienne
Krok 4: Zbuduj data.json w formacie Radoskop

Użycie:
    pip install requests beautifulsoup4 lxml
    python scrape_krakow.py [--output docs/data.json] [--profiles docs/profiles.json]

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny *.krakow.pl
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
from urllib.parse import parse_qs, urljoin, urlparse

# Wspólne liby z radoskop/scripts (lib_stenogram) dostępne w całym module.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

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

BIP_BASE = "https://www.bip.krakow.pl/"
SESSIONS_URL = f"{BIP_BASE}?bip_id=1&mmi=26527"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 0.3  # politeness sleep przed HTTP, cache hits pomijają

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


# HTML cache na dysku. Klucz: hash URL. BIP Krakowa nie wystawia ETag-ów
# ani Cache-Control, ale dla zakończonych głosowań/sesji zawartość jest
# stała. Stabilność oceniamy na poziomie scrape (sesje > N dni temu — cache,
# najnowsze N dni — refetch). Cache layer poniżej zapamiętuje WSZYSTKO,
# decyzja o (re)użyciu zapada w callerze przez `use_cache=...`.
_CACHE_DIR: Path | None = None

# Sesje plenarne starsze niż tyle dni traktujemy jako stabilne. BIP Krakowa
# nie modyfikuje stenogramów/wyników po finalizacji sesji (kilka dni max).
STABLE_AGE_DAYS = 2  # sesje >=2 dni cache, bo vote data zwykle publikowany w 24h


def _is_session_stable(date_str: str) -> bool:
    """True gdy data sesji YYYY-MM-DD jest >= STABLE_AGE_DAYS dni temu."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    age = (datetime.now() - dt).days
    return age >= STABLE_AGE_DAYS


def init_cache(cache_dir: str | None) -> None:
    """Ustawia katalog cache HTML. None = wyłącz cache (legacy behavior)."""
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


_BIP_UNAVAILABLE = "Chwilowy brak dostępu do wybranych informacji"


def fetch(url: str, use_cache: bool = True) -> BeautifulSoup:
    """Fetch a page (with disk cache) and return BeautifulSoup.

    use_cache=False wymusza HTTP request (np. dla świeżych sesji których
    zawartość może się zmienić).

    Strony BIP zwracające "Chwilowy brak dostępu" NIE są zapisywane do
    cache (i istniejący wpis w cache z taką odpowiedzią jest ignorowany),
    żeby tymczasowa niedostępność nie trwale zatruwała cache.
    """
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        content = cache_file.read_text(encoding="utf-8")
        if _BIP_UNAVAILABLE not in content:
            # Cache hit — bez HTTP, bez sleep
            return BeautifulSoup(content, "lxml")
        # Stary cache zawiera "brak dostępu" — usuń i refetchuj
        print(f"  Cache zawiera 'brak dostępu', usuwam i refetchuję: {url}")
        try:
            cache_file.unlink()
        except Exception:
            pass

    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    if cache_file and _BIP_UNAVAILABLE not in resp.text:
        # Tylko zapisz do cache jeśli strona ma prawidłową zawartość
        try:
            cache_file.write_text(resp.text, encoding="utf-8")
        except Exception:
            pass  # cache write failure nie blokuje scrape
    elif cache_file and _BIP_UNAVAILABLE in resp.text:
        print(f"    UWAGA: BIP zwrócił 'brak dostępu' — pomijam cache dla {url}")
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
    """Parse '25 Lutego 2026 r.' or '25 Lutego 2026' → '2026-02-25'."""
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

def scrape_session_list() -> list[dict]:
    """Fetch the session list page and extract all sessions."""
    # Indeks sesji NIGDY z cache — nowe sesje dochodzą na bieżąco
    soup = fetch(SESSIONS_URL, use_cache=False)
    sessions = []

    # Session links look like: "XLVI - (25 Lutego 2026 r.)"
    # They're <a> tags linking to session detail pages
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        # Match pattern: "ROMAN_NUMERAL - (DATE)"
        m = re.match(r'^([IVXLCDM]+)\s*-\s*\((.+?)\)$', text)
        if not m:
            continue

        number = m.group(1)
        date_str = m.group(2)
        date = parse_polish_date(date_str)
        if not date:
            print(f"  Nie udało się sparsować daty: '{date_str}'")
            continue

        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        sessions.append({
            "number": number,
            "date": date,
            "url": href,
        })

    # Deduplicate by number (in case of duplicate links)
    seen = set()
    unique = []
    for s in sessions:
        if s["number"] not in seen:
            seen.add(s["number"])
            unique.append(s)

    print(f"  Znaleziono {len(unique)} sesji")
    return sorted(unique, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape session page → find vote links
# ---------------------------------------------------------------------------

def scrape_session_votes_links(session: dict) -> list[dict]:
    """Fetch session page and find all voting detail links.

    Session pages have links to 'posiedzenie' pages (agenda).
    We first look for posiedzenie links, then from those pages find vote links.
    If the session URL already points to a posiedzenie, we find vote links directly.

    Stabilne sesje (date > 7 dni temu) używają cache HTML; najnowsze fresh.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    soup = fetch(session["url"], use_cache=is_stable)
    vote_links = []

    # The session page may directly list agenda items as links,
    # or it may have posiedzenie sub-pages.
    # Strategy: find all links with info=punkt or info=posiedzenie

    posiedzenie_links = []
    punkt_links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        parsed = urlparse(href)
        params = parse_qs(parsed.query)

        if "info" in params:
            info_val = params["info"][0]
            if info_val == "posiedzenie" and "PSD_ID" in params:
                posiedzenie_links.append({"url": href, "text": text})
            elif info_val == "punkt" and "PSS_ID" in params:
                pss_id = params["PSS_ID"][0]
                punkt_links.append({
                    "url": href,
                    "text": text,
                    "pss_id": pss_id,
                })

    # If we found punkt links directly, use them
    if punkt_links:
        return punkt_links

    # Otherwise, follow posiedzenie links to find punkt links
    for pos in posiedzenie_links:
        pos_soup = fetch(pos["url"], use_cache=is_stable)
        for a in pos_soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not href.startswith("http"):
                href = urljoin(BIP_BASE, href)

            parsed = urlparse(href)
            params = parse_qs(parsed.query)

            if "info" in params and params["info"][0] == "punkt" and "PSS_ID" in params:
                pss_id = params["PSS_ID"][0]
                punkt_links.append({
                    "url": href,
                    "text": text,
                    "pss_id": pss_id,
                })

    # Deduplicate by PSS_ID
    seen_pss = set()
    unique_links = []
    for pl in punkt_links:
        if pl["pss_id"] not in seen_pss:
            seen_pss.add(pl["pss_id"])
            unique_links.append(pl)

    return unique_links


# ---------------------------------------------------------------------------
# Step 3: Scrape individual vote page
# ---------------------------------------------------------------------------

def scrape_vote_detail(vote_url: str, session: dict, vote_idx: int) -> dict | None:
    """Parse a vote detail page.

    BIP Kraków HTML structure (all in one block, <br>-separated):
      <h1>Głosowania dotyczące punktu porządku obrad (IX kadencja RMK):</h1>
      <h2>Topic / projektodawca ... / Druk nr 123</h2>
      <h3>Przedmiot głosowania: DRUK NR 123</h3><br>
      Czas rozpoczęcia: ...<br>
      Głosów za: 34<br>
      ...<br>
      <b>UCHWAŁA PODJĘTA</b><br>
      Jak głosowali radni:<br>
      Anna Bałdyga - <b>Za<br></b>Iwona Chamielec - <b>Przeciw<br></b>...

    Stabilne głosowania (sesja > 7 dni temu) używają cache HTML.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    soup = fetch(vote_url, use_cache=is_stable)

    # --- Topic from <h2> ---
    topic = ""
    druk = None
    h2 = soup.find("h2")
    if h2:
        topic = h2.get_text(strip=True)

    # Extract druk number
    raw_html = str(soup)
    druk_match = re.search(r'[Dd]ruk\s+(?:nr\s+)?(\d+[\w-]*)', topic or raw_html)
    if druk_match:
        druk = druk_match.group(1)

    # --- Vote counts (from raw HTML to avoid text extraction issues) ---
    counts = {
        "za": 0,
        "przeciw": 0,
        "wstrzymal_sie": 0,
        "brak_glosu": 0,
        "nieobecni": 0,
    }

    za_match = re.search(r'Głosów\s+za:\s*(\d+)', raw_html)
    przeciw_match = re.search(r'Głosów\s+przeciw:\s*(\d+)', raw_html)
    wstrzymal_match = re.search(r'Głosów\s+wstrzymujących\s+się:\s*(\d+)', raw_html)
    nieobecni_match = re.search(r'Nieobecnych:\s*(\d+)', raw_html)
    brak_match = re.search(r'Nie\s+brało\s+udziału\s+w\s+głosowaniu:\s*(\d+)', raw_html)

    if za_match:
        counts["za"] = int(za_match.group(1))
    if przeciw_match:
        counts["przeciw"] = int(przeciw_match.group(1))
    if wstrzymal_match:
        counts["wstrzymal_sie"] = int(wstrzymal_match.group(1))
    if nieobecni_match:
        counts["nieobecni"] = int(nieobecni_match.group(1))
    if brak_match:
        counts["brak_glosu"] = int(brak_match.group(1))

    # --- Resolution status ---
    resolution = None
    upper_html = raw_html.upper()
    if "UCHWAŁA PODJĘTA" in upper_html and "UCHWAŁA NIEPODJĘTA" not in upper_html and "UCHWAŁA NIE PODJĘTA" not in upper_html:
        resolution = "przyjęta"
    elif "UCHWAŁA NIEPODJĘTA" in upper_html or "UCHWAŁA NIE PODJĘTA" in upper_html:
        resolution = "odrzucona"

    # --- Individual votes ---
    # HTML format: "Name - <b>Za<br></b>Name2 - <b>Przeciw<br></b>"
    # Strategy: extract the text after "Jak głosowali radni:" from raw HTML,
    # replace <br> and </b> with newlines, then parse "Name - Vote" lines.
    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    # Find the votes section in raw HTML
    votes_start = raw_html.find("Jak głosowali radni:")
    if votes_start == -1:
        # Try alternate marker
        votes_start = raw_html.find("Jak g&#322;osowali radni:")

    if votes_start != -1:
        # Extract from "Jak głosowali radni:" to end of content area
        votes_html = raw_html[votes_start:]
        # Cut at footer/label section
        for end_marker in ["<div class=\"labelBox\"", "<div class=\"addCont\"",
                           "<div class=\"bottommenu\"", "Ochrona danych osobowych"]:
            end_pos = votes_html.find(end_marker)
            if end_pos != -1:
                votes_html = votes_html[:end_pos]
                break

        # BIP pages often include multiple voting rounds on one page.
        # Only take the FIRST round — from "Jak głosowali radni:" to the
        # next occurrence (which belongs to a different vote on the same page).
        next_round = votes_html.find("Jak głosowali radni:", 20)
        if next_round == -1:
            # Also check HTML-encoded version
            next_round = votes_html.find("Jak g&#322;osowali radni:", 20)
        if next_round != -1:
            votes_html = votes_html[:next_round]

        # Replace HTML tags with newlines to get clean text
        votes_text = votes_html
        votes_text = re.sub(r'<br\s*/?>', '\n', votes_text)
        votes_text = re.sub(r'</?b>', '', votes_text)
        votes_text = re.sub(r'<[^>]+>', ' ', votes_text)
        # Clean up HTML entities
        votes_text = votes_text.replace('&nbsp;', ' ')
        votes_text = votes_text.replace('&amp;', '&')

        vote_pattern = re.compile(
            r'(.+?)\s*-\s*(Za|Przeciw|Wstrzymuje się|Nie głosował|Nieobecn[ayi]?)\s*$',
            re.IGNORECASE
        )

        for line in votes_text.split('\n'):
            line = line.strip()
            # Normalize multiple spaces
            line = re.sub(r'\s+', ' ', line)
            if not line:
                continue

            m = vote_pattern.match(line)
            if m:
                name = m.group(1).strip()
                vote_type = m.group(2).strip().lower()

                # Skip non-name lines that accidentally match
                if len(name) < 3 or name.startswith("Jak głos"):
                    continue

                if vote_type == "za":
                    named_votes["za"].append(name)
                elif vote_type == "przeciw":
                    named_votes["przeciw"].append(name)
                elif vote_type == "wstrzymuje się":
                    named_votes["wstrzymal_sie"].append(name)
                elif vote_type == "nie głosował":
                    named_votes["brak_glosu"].append(name)
                elif vote_type.startswith("nieobecn"):
                    named_votes["nieobecni"].append(name)

    # Deduplicate: (1) within each category, (2) across categories
    # BIP pages sometimes include multiple voting rounds on one page.
    for cat in named_votes:
        named_votes[cat] = list(dict.fromkeys(named_votes[cat]))

    # If same person appears in multiple categories (from merged rounds),
    # keep only their first occurrence.
    seen_names: set[str] = set()
    for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]:
        filtered = []
        for name in named_votes[cat]:
            if name not in seen_names:
                seen_names.add(name)
                filtered.append(name)
        named_votes[cat] = filtered

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        print(f"    UWAGA: Brak głosów imiennych na {vote_url}")
        return None

    # Session number in vote_id avoids collisions when two sessions share a
    # date (Krakow has 2025-06-11 and 2024-06-19 with multiple sessions).
    session_num = session.get("number") or ""
    num_part = f"_{session_num}" if session_num else ""
    vote_id = f"{session['date']}{num_part}_{vote_idx:03d}"

    return {
        "id": vote_id,
        "source_url": vote_url,
        "session_date": session["date"],
        "session_number": session["number"],
        "topic": topic[:500] if topic else f"Głosowanie {vote_idx}",
        "druk": druk,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


# ---------------------------------------------------------------------------
# Step 4: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping."""
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path} — kluby będą oznaczone jako '?'")
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = {}
    for p in data.get("profiles", []):
        name = p["name"]
        kadencje = p.get("kadencje", {})
        if kadencje:
            latest = list(kadencje.values())[-1]
            result[name] = {
                "name": name,
                "club": latest.get("club", "?"),
                "district": latest.get("okręg"),
            }
    return result


def compute_club_majority(vote: dict, profiles: dict) -> dict[str, str]:
    """For each club, compute the majority position in a given vote."""
    club_votes = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    for cat in ["za", "przeciw", "wstrzymal_sie"]:
        for name in vote["named_votes"].get(cat, []):
            club = profiles.get(name, {}).get("club", "?")
            if club != "?":
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

    # Only count sessions that have vote data for frekwencja calculation
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


# ---------------------------------------------------------------------------
# Step 5: Transcript scraping
# ---------------------------------------------------------------------------

MATERIALY_URL = f"{BIP_BASE}?dok_id=190813"


def scrape_stenogram_links() -> dict[str, str]:
    """Scrape the Materiały sesyjne page for stenogram PDF links.

    Returns: {session_date: stenogram_doc_id}
    """
    # Materiały sesyjne — top-level indeks, NIGDY z cache (dochodzą stenogramy)
    soup = fetch(MATERIALY_URL, use_cache=False)
    raw_html = str(soup)

    results = {}

    # Each session block has:
    #   <strong>DD.MM.YY r. - ROMAN sesja ... </strong>
    #   <a href="...">Stenogram</a>
    # Find all <p> or block elements containing session info + stenogram link
    session_blocks = re.findall(
        r'(\d{2}\.\d{2}\.\d{2,4})\s+r\.\s*[-–]\s*([IVXLCDM]+)\s+'
        r'(?:uroczysta\s+)?sesj[aią].*?'
        r'(?:<a[^>]+href="([^"]*)"[^>]*>Stenogram</a>|Stenogram)',
        raw_html, re.DOTALL | re.IGNORECASE
    )

    for date_str, number, steno_href in session_blocks:
        # Parse date: DD.MM.YY or DD.MM.YYYY
        parts = date_str.split(".")
        if len(parts) == 3:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2])
            if year < 100:
                year += 2000
            date = f"{year}-{month:02d}-{day:02d}"

            if steno_href:
                # Extract doc_id from URL like ".../n/654709/karta"
                doc_match = re.search(r'/n/(\d+)/', steno_href)
                if doc_match:
                    doc_id = doc_match.group(1)
                    results[date] = doc_id
                    print(f"    {number} ({date}): stenogram doc_id={doc_id}")

    print(f"  Znaleziono {len(results)} stenogramów")
    return results


def download_stenogram(doc_id: str, cache_dir: Path) -> Path | None:
    """Download a stenogram PDF via plik.php.

    URL format: https://www.bip.krakow.pl/plik.php?zid=DOC_ID&wer=0&new=t&mode=shw
    """
    filename = f"stenogram_{doc_id}.pdf"
    path = cache_dir / filename

    if path.exists() and path.stat().st_size > 1000:
        print(f"    Cache hit: {filename}")
        return path

    url = f"{BIP_BASE}plik.php?zid={doc_id}&wer=0&new=t&mode=shw"
    time.sleep(DELAY)
    print(f"    GET {url}")
    try:
        resp = _session.get(url, timeout=60)
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


def process_transcripts(stenogram_links: dict[str, str], cache_dir: Path,
                        profiles_lookup: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Download and parse all stenogram PDFs.

    Returns (session_speakers, session_turns):
      session_speakers: {date: [{"name", "statements", "words"}, ...]}  (agregat)
      session_turns:    {date: [{"name", "text", "words", "role"}, ...]} (pełne tury)
    """
    from parse_stenogram import parse_turns
    from lib_stenogram import aggregate_speakers

    session_speakers: dict[str, list[dict]] = {}
    session_turns: dict[str, list[dict]] = {}
    exact = (profiles_lookup or {}).get("_exact", {})

    for date, doc_id in sorted(stenogram_links.items()):
        print(f"\n  Stenogram {date} (doc_id={doc_id})")
        pdf_path = download_stenogram(doc_id, cache_dir)
        if not pdf_path:
            continue

        try:
            turns = parse_turns(str(pdf_path), profiles_lookup)
            speakers = aggregate_speakers(turns)
            total_words = sum(s["words"] for s in speakers)
            councilor_count = sum(1 for s in speakers if s["name"] in exact)
            print(f"    Sparsowano: {len(turns)} tur, {len(speakers)} mówców "
                  f"({councilor_count} radnych), {total_words} słów")
            if turns:
                session_turns[date] = turns
                session_speakers[date] = [
                    {"name": s["name"], "statements": s["statements"], "words": s["words"]}
                    for s in speakers
                ]
        except Exception as e:
            print(f"    BŁĄD parsowania: {e}")

    return session_speakers, session_turns


def write_session_transcripts(sessions_data: list[dict], session_turns: dict[str, list[dict]],
                              out_dir, kid: str, councilors: list[dict],
                              stenogram_links: dict[str, str] | None = None) -> int:
    """Zapisz pełne stenogramy per sesja do docs/transcripts/{kid}/{num}.json.

    Ustawia sd["has_transcript"] i sd["transcript_word_count"] na sesjach.
    """
    from lib_stenogram import build_transcript, write_transcript

    club_lookup = {c["name"]: c.get("club") for c in councilors}
    written = 0
    for sd in sessions_data:
        turns = session_turns.get(sd["date"])
        if not turns:
            continue
        meta = {
            "city": "krakow", "city_name": "Kraków", "kadencja": kid,
            "session_number": sd["number"], "date": sd["date"],
        }
        doc_id = (stenogram_links or {}).get(sd["date"])
        if doc_id:
            meta["source_url"] = (
                f"https://www.bip.krakow.pl/plik.php?zid={doc_id}&wer=0&new=t&mode=shw"
            )
        tr = build_transcript(meta, turns, club_lookup)
        write_transcript(out_dir, kid, sd["number"], tr)
        sd["has_transcript"] = True
        sd["transcript_word_count"] = tr["stats"]["total_words"]
        written += 1

    print(f"  Stenogramy zapisane: {written} → {out_dir}/transcripts/{kid}/")
    return written


def integrate_activity(councilors: list[dict], sessions_data: list[dict],
                       session_speakers: dict[str, list[dict]]) -> None:
    """Add activity data from transcripts to councilors and sessions."""
    # Add speakers to session data
    for sd in sessions_data:
        sp = session_speakers.get(sd["date"], [])
        sd["speakers"] = sp

    # Build activity per councilor
    known_names = {c["name"] for c in councilors}
    councilor_activity: dict[str, dict] = {}

    for date, speakers in session_speakers.items():
        session_number = ""
        for sd in sessions_data:
            if sd["date"] == date:
                session_number = sd.get("number", "")
                break

        for s in speakers:
            if s["name"] not in known_names:
                continue
            if s["name"] not in councilor_activity:
                councilor_activity[s["name"]] = {
                    "sessions": [], "total_statements": 0, "total_words": 0
                }
            act = councilor_activity[s["name"]]
            act["sessions"].append({
                "date": date,
                "session": session_number,
                "statements": s["statements"],
                "words": s["words"],
            })
            act["total_statements"] += s["statements"]
            act["total_words"] += s["words"]

    # Merge activity into councilors
    has_data = bool(session_speakers)
    for c in councilors:
        act = councilor_activity.get(c["name"])
        if act:
            sessions_spoke = len(act["sessions"])
            c["has_activity_data"] = True
            c["activity"] = {
                "sessions_spoke": sessions_spoke,
                "total_statements": act["total_statements"],
                "total_words": act["total_words"],
                "avg_statements_per_session": round(act["total_statements"] / sessions_spoke, 1),
                "avg_words_per_session": round(act["total_words"] / sessions_spoke),
                "sessions": act["sessions"],
            }
        else:
            c["has_activity_data"] = has_data
            c["activity"] = None

    spoke_count = sum(1 for c in councilors if c.get("activity"))
    print(f"  Aktywność: {spoke_count}/{len(councilors)} radnych z wypowiedziami")


def merge_stats_to_profiles(profiles_path: str, output: dict):
    """Merge voting stats from data.json councilors into profiles.json."""
    path = Path(profiles_path)
    if not path.exists():
        print("  Pominięto merge — brak profiles.json")
        return

    with open(path, encoding="utf-8") as f:
        profiles = json.load(f)

    stats = {}
    for kad in output["kadencje"]:
        kid = kad["id"]
        for c in kad["councilors"]:
            stats[(kid, c["name"])] = c

    updated = 0
    for p in profiles.get("profiles", []):
        if "kadencje" not in p:
            p["kadencje"] = {}

        for kid, entry in p["kadencje"].items():
            c = stats.get((kid, p["name"]))
            if not c:
                continue
            for key in ["frekwencja", "aktywnosc", "zgodnosc_z_klubem",
                        "votes_za", "votes_przeciw", "votes_wstrzymal",
                        "votes_brak", "votes_nieobecny", "votes_total",
                        "rebellion_count", "rebellions"]:
                if key in c:
                    entry[key] = c[key]
            if not entry.get("club") and c.get("club"):
                entry["club"] = c["club"]
            entry["has_voting_data"] = True
            entry["has_activity_data"] = c.get("has_activity_data", False)
            if c.get("activity"):
                entry["activity"] = c["activity"]
            elif "activity" in entry:
                del entry["activity"]
            updated += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"  Zaktualizowano profiles.json: {updated} wpisów")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Krakowa (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--explore", action="store_true", help="Pobierz 1 sesję i pokaż strukturę")
    parser.add_argument("--transcripts", action="store_true", help="Pobierz i parsuj stenogramy sesji")
    parser.add_argument("--only-transcripts", action="store_true",
                        help="Tylko stenogramy (wymaga istniejącego data.json)")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog cache HTML stron BIP. Domyślnie {script_dir}/../.cache/html. "
                             "Stabilne sesje (>7 dni temu) używają cache zamiast HTTP. "
                             "Ustaw '' aby wyłączyć cache.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Wyłącz cache HTML (każdy run = świeże HTTP wszystkim sesjom).")
    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    print("=== Radoskop Scraper: Rada Miasta Krakowa (BIP) ===")
    print(f"Backend: requests + BeautifulSoup")
    if args.transcripts or args.only_transcripts:
        print("Tryb: z transkrypcjami stenogramów")
    print()

    init_session()

    # Cache HTML — default poza repo Radoskop (workdir scriptu/../.cache/html/)
    # Sandbox cities/krakow/.cache/* jest w .gitignore. Persistent na NAS-ie
    # bo katalog `cities/*/.cache/` jest untracked.
    if args.no_cache:
        cache_dir = None
    elif args.cache_dir is not None:
        cache_dir = args.cache_dir or None  # pusty string '' wyłącza
    else:
        cache_dir = str(Path(__file__).resolve().parent.parent / ".cache" / "html")
    init_cache(cache_dir)
    if cache_dir:
        print(f"Cache HTML: {cache_dir}")
    print()

    # Handle --only-transcripts: load existing data.json, add transcripts, save
    if args.only_transcripts:
        out_path = Path(args.output)
        if not out_path.exists():
            print(f"BŁĄD: --only-transcripts wymaga istniejącego {args.output}")
            sys.exit(1)

        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)

        print("[1/2] Pobieranie linków do stenogramów...")
        steno_links = scrape_stenogram_links()

        # Build profiles lookup for name resolution
        from parse_stenogram import build_profiles_lookup
        profiles_path = Path(args.profiles)
        profiles_lookup = None
        if profiles_path.exists():
            with open(profiles_path, encoding="utf-8") as f:
                profiles_lookup = build_profiles_lookup(json.load(f))

        # Cache PDF stenogramów w scratch (obok cache HTML), NIE w docs/ —
        # inaczej deploy zsynchronizowałby PDF-y na S3. Fallback: obok output.
        cache_dir = (Path(args.cache_dir).parent / "transcript_cache"
                     if args.cache_dir else out_path.parent / "transcript_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[2/2] Pobieranie i parsowanie stenogramów...")
        session_speakers, session_turns = process_transcripts(steno_links, cache_dir, profiles_lookup)

        # Integrate into existing data
        for kad in existing["kadencje"]:
            integrate_activity(kad["councilors"], kad["sessions"], session_speakers)
            write_session_transcripts(kad["sessions"], session_turns, out_path.parent,
                                      kad["id"], kad["councilors"], steno_links)

        existing["generated"] = datetime.now().isoformat()
        save_split_output(existing, out_path)
        print(f"\nGotowe! Zaktualizowano {out_path}")

        # Also merge into profiles
        merge_stats_to_profiles(args.profiles, existing)
        return

    total_steps = 4 if (args.transcripts) else 3

    # 1. Session list
    print(f"[1/{total_steps}] Pobieranie listy sesji...")
    all_sessions = scrape_session_list()

    if not all_sessions:
        print("BŁĄD: Nie znaleziono sesji.")
        print(f"Sprawdź ręcznie: {SESSIONS_URL}")
        sys.exit(1)

    if args.max_sessions > 0:
        all_sessions = all_sessions[:args.max_sessions]
        print(f"  (ograniczono do {args.max_sessions} sesji)")

    if args.dry_run:
        print("\nZnalezione sesje:")
        for s in all_sessions:
            print(f"  {s['number']:>8} | {s['date']} | {s['url']}")
        return

    if args.explore:
        s0 = all_sessions[-1]  # latest session
        print(f"\n[explore] Sesja {s0['number']} ({s0['date']})")
        print(f"  URL: {s0['url']}")
        soup = fetch(s0["url"])
        print("\n--- Linki na stronie sesji ---")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)[:120]
            if text and not href.startswith("javascript") and not href.startswith("#"):
                print(f"  [{text}]")
                print(f"    -> {href}")
        return

    # 2. Fetch votes for each session
    print(f"\n[2/{total_steps}] Pobieranie głosowań ({len(all_sessions)} sesji)...")
    all_votes = []
    seen_pss_ids: set[str] = set()  # Global dedup across sessions
    dupes_skipped = 0
    for si, session in enumerate(all_sessions):
        print(f"\n  Sesja {session['number']} ({session['date']}) [{si+1}/{len(all_sessions)}]")

        vote_links = scrape_session_votes_links(session)
        # Filter out PSS_IDs already seen from other sessions
        new_links = []
        for vl in vote_links:
            if vl["pss_id"] not in seen_pss_ids:
                seen_pss_ids.add(vl["pss_id"])
                new_links.append(vl)
            else:
                dupes_skipped += 1
        print(f"    Znaleziono {len(vote_links)} głosowań ({len(vote_links) - len(new_links)} duplikatów)")

        for vi, vl in enumerate(new_links):
            vote = scrape_vote_detail(vl["url"], session, vi + 1)
            if vote:
                all_votes.append(vote)

        print(f"    Sparsowano {sum(1 for v in all_votes if v['session_date'] == session['date'])} głosowań")

    if dupes_skipped:
        print(f"\n  Pominięto {dupes_skipped} duplikatów PSS_ID między sesjami")
    print(f"  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        sys.exit(1)

    # 3. Transcripts (optional)
    session_speakers: dict[str, list[dict]] = {}
    session_turns: dict[str, list[dict]] = {}
    steno_links: dict[str, str] = {}
    if args.transcripts:
        print(f"\n[3/{total_steps}] Pobieranie stenogramów...")
        steno_links = scrape_stenogram_links()

        from parse_stenogram import build_profiles_lookup

        profiles_path = Path(args.profiles)
        profiles_lookup = None
        if profiles_path.exists():
            with open(profiles_path, encoding="utf-8") as f:
                profiles_lookup = build_profiles_lookup(json.load(f))

        # Cache PDF stenogramów w scratch (obok cache HTML), NIE w docs/.
        cache_dir = (Path(args.cache_dir).parent / "transcript_cache"
                     if args.cache_dir else Path(args.output).parent / "transcript_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        session_speakers, session_turns = process_transcripts(steno_links, cache_dir, profiles_lookup)
        print(f"  Transkrypcje: {len(session_speakers)}/{len(all_sessions)} sesji")

    # Build output
    build_step = 4 if args.transcripts else 3
    print(f"\n[{build_step}/{total_steps}] Budowanie pliku wyjściowego...")
    profiles = load_profiles(args.profiles)
    if profiles:
        print(f"  Załadowano profile: {len(profiles)} radnych")

    kid = "2024-2029"
    councilors = build_councilors(all_votes, all_sessions, profiles)
    sessions_data = build_sessions(all_sessions, all_votes)
    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"  Kluby: {dict(club_counts)}")

    # Integrate transcript activity data
    if session_speakers:
        integrate_activity(councilors, sessions_data, session_speakers)
    if session_turns:
        write_session_transcripts(sessions_data, session_turns, Path(args.output).parent,
                                  kid, councilors, steno_links)

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

    out_path = Path(args.output)
    save_split_output(output, out_path)

    print(f"\nGotowe! Zapisano do {out_path}")
    total_v = len(all_votes)
    named_v = sum(1 for v in all_votes if sum(len(nv) for nv in v["named_votes"].values()) > 0)
    print(f"  {len(sessions_data)} sesji, {total_v} głosowań ({named_v} z imiennymi), {len(councilors)} radnych")

    # Merge stats into profiles.json
    merge_stats_to_profiles(args.profiles, output)


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


if __name__ == "__main__":
    main()
