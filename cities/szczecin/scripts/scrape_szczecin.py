#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Szczecina.

Źródło: BIP Szczecin (bip.um.szczecin.pl)
BIP Szczecin to standardowy HTML — nie wymaga JavaScript.
Używa requests + BeautifulSoup do scrapowania.

Struktura BIP:
  1. Lista sesji: https://bip.um.szczecin.pl/chapter_50509 (sesje z wynikami głosowań)
  2. Sesja (artykuł): /artykul/ID/sesja-nr-... (strona sesji)
  3. Wyniki głosowań (tabele HTML): wbudowane w stronę sesji

Krok 1: Pobierz listę sesji
Krok 2: Dla każdej sesji — pobierz stronę i parsuj tabele głosowań
Krok 3: Ekstraktuj wyniki imienne z tabel
Krok 4: Zbuduj data.json w formacie Radoskop

Użycie:
    pip install requests beautifulsoup4 lxml
    python scrape_szczecin.py [--output docs/data.json] [--profiles docs/profiles.json]

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


BIP_BASE = "https://bip.um.szczecin.pl/"
SESSIONS_URL = f"{BIP_BASE}chapter_50509.asp?kadencja=IX"
ESESJA_BASE = "https://szczecin.esesja.pl"
ESESJA_ARCHIVE = f"{ESESJA_BASE}/glosowania"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 1.0

# Radni Szczecina IX kadencja (2024-2029)
# Struktura: {imię + nazwisko: klub}
# Źródło: BIP Szczecin (bip.um.szczecin.pl/chapter_50591.asp)
# 33 radnych: 21x KO, 7x PiS, 5x OK
COUNCILORS = {
    # KO - Koalicja Obywatelska (19 radnych)
    "Abramowicz Elżbieta": "KO",
    "Bartnik Paweł": "KO",
    "Biskupski Marcin": "KO",
    "Bohuń Maria": "KO",
    "Dorżynkiewicz Wojciech": "KO",
    "Gieryga Mateusz": "KO",
    "Gródecka Szwajkiewicz Dorota": "KO",
    "Herczyński Roman": "KO",
    "Jasińska Ewa": "KO",
    "Jeleniewska Zuzanna": "KO",
    "Kaup Stanisław": "KO",
    "Kępka Wojciech": "KO",
    "Milewska Ilona": "KO",
    "Pańka Urszula": "KO",
    "Posłuszny Jan": "KO",
    "Radziwinowicz Andrzej": "KO",
    "Rogaczewska Wiktoria": "KO",
    "Schneider Maria": "KO",
    "Słowik Przemysław": "KO",
    "Tyszler Łukasz": "KO",
    "Wleklak Małgorzata": "KO",

    # PiS - Prawo i Sprawiedliwość (7 radnych)
    "Chabior Marek": "PiS",
    "Duklanowski Marek": "PiS",
    "Kopeć Maciej": "PiS",
    "Pawlicki Marcin": "PiS",
    "Romianowski Krzysztof": "PiS",
    "Smoliński Dariusz": "PiS",
    "Szałabawka Julia": "PiS",

    # OK - OK Polska (5 radnych)
    "Balicka Jolanta": "OK",
    "Jerzyk Henryk": "OK",
    "Kęsik Piotr": "OK",
    "Kolbowicz Marek": "OK",
    "Łażewska Renata": "OK",
}

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
    return (datetime.now() - dt).days >= STABLE_AGE_DAYS


def fetch(url: str, use_cache: bool = True) -> BeautifulSoup:
    """Fetch a page (with disk cache) and return BeautifulSoup."""
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        text = cache_file.read_text(encoding="utf-8")
        return BeautifulSoup(text, "lxml")

    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    # eSesja pages declare windows-1250 but requests detects ISO-8859-1
    if "esesja" in url:
        resp.encoding = "windows-1250"
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
    """Parse '25 Marca 2026 r.' or '25 Marca 2026' → '2026-03-25'."""
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
    """Fetch the session list page and extract all sessions.

    BIP Szczecin format: table with rows like:
      <td>2024-05-07</td><td><a href="...?kadencja=IX&sesja=408">inauguracyjna sesja Rady Miasta Szczecin</a></td>
    Session number is extracted from link text:
      "inauguracyjna sesja" -> I, "II zwyczajna sesja" -> II, "III nadzwyczajna sesja" -> III
    """
    # Lista sesji NIGDY z cache — nowe sesje dochodzą
    soup = fetch(SESSIONS_URL, use_cache=False)
    sessions = []

    # Find session links inside table rows
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "sesja=" not in href:
            continue

        text = a.get_text(strip=True)
        if "sesja" not in text.lower():
            continue

        # Extract session number from text
        # "inauguracyjna sesja" = I, "II zwyczajna sesja" = II
        num_m = re.match(r'^([IVXLCDM]+)\s+', text)
        if num_m:
            number = num_m.group(1).upper()
        elif "inauguracyjna" in text.lower():
            number = "I"
        else:
            continue

        # Skip ceremonial sessions (uroczysta)
        if "uroczysta" in text.lower():
            continue

        # Find date in sibling/parent td
        date = None
        td = a.find_parent("td")
        if td:
            row = td.find_parent("tr")
            if row:
                cells = row.find_all("td")
                for cell in cells:
                    cell_text = cell.get_text(strip=True)
                    dm = re.match(r'(\d{4}-\d{2}-\d{2})', cell_text)
                    if dm:
                        date = dm.group(1)
                        break

        if not date:
            continue

        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        sessions.append({
            "number": number,
            "date": date,
            "url": href,
        })

    # Deduplicate by (number, date)
    seen = set()
    unique = []
    for s in sessions:
        key = (s["number"], s["date"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    # Filter by kadencja start date
    kadencja_start = KADENCJE["2024-2029"]["start"]
    filtered = [s for s in unique if s["date"] >= kadencja_start]
    print(f"  Znaleziono {len(unique)} sesji ogółem, {len(filtered)} w kadencji 2024-2029")

    if not filtered and unique:
        print(f"  UWAGA: Brak sesji po {kadencja_start}.")
        print(f"  Najnowsza znaleziona: {max(s['date'] for s in unique)}")
        return sorted(unique, key=lambda x: x["date"])

    return sorted(filtered, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape session page → find vote tables
# ---------------------------------------------------------------------------

def fetch_esesja_session_map() -> dict:
    """Fetch eSesja voting archive and build date → vote list URL map.

    Returns {session_date: esesja_url} so we can match BIP sessions to eSesja.
    """
    # Archiwum eSesja — top-level index, NIGDY z cache
    soup = fetch(ESESJA_ARCHIVE, use_cache=False)
    session_map = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/listaglosowan/" not in href:
            continue

        text = a.get_text(strip=True)
        # Extract date from text like "...w dniu 27 stycznia 2026, godz..."
        m = re.search(r'w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})', text)
        if not m:
            continue

        day = int(m.group(1))
        month_name = m.group(2).lower()
        year = int(m.group(3))
        month = MONTHS_PL.get(month_name)
        if not month:
            continue

        date = f"{year}-{month:02d}-{day:02d}"
        url = href if href.startswith("http") else ESESJA_BASE + href
        session_map[date] = url

    print(f"  eSesja: znaleziono {len(session_map)} sesji z głosowaniami")
    return session_map


def scrape_session_votes(session: dict, esesja_url: str) -> list[dict]:
    """Fetch eSesja vote list page and parse individual votes.

    eSesja page at listaglosowan/UUID lists all votes for a session with links
    to individual vote pages at /glosowanie/ID/HASH.

    Stabilne sesje (date > 7 dni temu) używają cache HTML.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    soup = fetch(esesja_url, use_cache=is_stable)
    votes = []

    # Collect unique vote links: /glosowanie/ID/HASH
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
        # Topic will be extracted from individual vote page
        vote_links.append({"url": url, "topic": ""})

    print(f"    Znaleziono {len(vote_links)} linków do głosowań")

    for idx, vl in enumerate(vote_links):
        vote = scrape_single_vote(vl["url"], session, idx, vl["topic"])
        if vote:
            votes.append(vote)
        time.sleep(DELAY * 0.5)

    print(f"    Wyodrębniono {len(votes)} głosowań z imiennymi wynikami")
    return votes


def scrape_single_vote(url: str, session: dict, vote_idx: int, topic: str) -> dict | None:
    """Fetch a single eSesja vote page and parse named results.

    eSesja HTML structure:
      <div class='wim'><h3>ZA<span class='za'> (30)</span></h3>
        <div class='osobaa'>Surname FirstName</div>
        ...
      </div>
      <div class='wim'><h3>PRZECIW<span class='przeciw'> (0)</span></h3></div>
      ...

    Stabilne sesje (date > 7 dni temu) używają cache HTML.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    try:
        soup = fetch(url, use_cache=is_stable)
    except Exception as e:
        print(f"      Błąd pobierania {url}: {e}")
        return None

    # Extract topic from h1 (format: "Wyniki głosowania jawnego w sprawie: TOPIC")
    if not topic:
        h1 = soup.find("h1")
        if h1:
            topic = h1.get_text(strip=True)[:500]
        else:
            topic = f"Głosowanie {vote_idx + 1}"

    # Clean topic: remove eSesja prefixes
    topic = re.sub(r'^Wyniki głosowania jawnego w sprawie:\s*', '', topic).strip()
    topic = re.sub(r'^Wyniki głosowania w sprawie:?\s*', '', topic).strip()
    topic = re.sub(r'^Głosowanie\s+w\s+sprawie\s+', '', topic).strip()
    if not topic:
        topic = f"Głosowanie {vote_idx + 1}"

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

    # Extract counts from summary div (class='podsumowanie')
    summary = soup.find("div", class_="podsumowanie")
    if summary:
        for cls, key in [("za", "za"), ("przeciw", "przeciw"),
                         ("wstrzymuje", "wstrzymal_sie"),
                         ("brakglosu", "brak_glosu"),
                         ("nieobecni", "nieobecni")]:
            div = summary.find("div", class_=cls)
            if div:
                span = div.find("span")
                if span:
                    try:
                        counts[key] = int(span.get_text(strip=True))
                    except ValueError:
                        pass

    # Parse named votes from div.wim sections.
    # Each div.wim has a h3 header (ZA/PRZECIW/...) and div.osobaa children.
    # Some eSesja instances put the category as a CSS class on div.osobaa,
    # others only on the parent div.wim header.
    header_to_cat = {
        "za": "za",
        "przeciw": "przeciw",
        "wstrzymuj": "wstrzymal_sie",
        "brak g": "brak_glosu",
        "nieobecn": "nieobecni",
    }

    class_to_cat = {
        "za": "za",
        "przeciw": "przeciw",
        "wstrzymuje": "wstrzymal_sie",
        "nieobecny": "nieobecni",
        "nieobecni": "nieobecni",
        "brakglosu": "brak_glosu",
    }

    # Strategy 1: parse by div.wim sections (header determines category)
    for wim in soup.find_all("div", class_="wim"):
        h3 = wim.find("h3")
        if not h3:
            continue
        h3_text = h3.get_text(strip=True).lower()
        cat_key = None
        for prefix, cat in header_to_cat.items():
            if h3_text.startswith(prefix):
                cat_key = cat
                break
        if not cat_key:
            continue
        for osoba in wim.find_all("div", class_="osobaa"):
            name = osoba.get_text(strip=True)
            if name and len(name) > 2:
                named_votes[cat_key].append(name)

    # Strategy 2 fallback: if div.wim parsing found nothing, try CSS classes on osobaa
    total_from_wim = sum(len(v) for v in named_votes.values())
    if total_from_wim == 0:
        for osoba in soup.find_all("div", class_="osobaa"):
            name = osoba.get_text(strip=True)
            if not name or len(name) <= 2:
                continue
            classes = osoba.get("class", [])
            cat_key = None
            for cls in classes:
                if cls in class_to_cat:
                    cat_key = class_to_cat[cls]
                    break
            if cat_key:
                named_votes[cat_key].append(name)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        return None

    # Update counts from named votes if summary parsing missed them
    for cat in named_votes:
        if counts[cat] == 0 and named_votes[cat]:
            counts[cat] = len(named_votes[cat])

    vote_id = f"{session['date']}_{vote_idx:03d}"

    return {
        "id": vote_id,
        "source_url": url,
        "session_date": session["date"],
        "session_number": session["number"],
        "topic": topic[:500],
        "druk": None,
        "resolution": None,
        "counts": counts,
        "named_votes": named_votes,
    }


# ---------------------------------------------------------------------------
# Step 3: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping.

    Club assignments from the COUNCILORS dict take priority over
    whatever is stored in profiles.json (to fix stale '?' values).
    """
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
            club = COUNCILORS.get(name, latest.get("club", "?"))
            result[name] = {
                "name": name,
                "club": club,
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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Szczecina (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--explore", action="store_true", help="Pobierz 1 sesję i pokaż strukturę")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog cache HTML. Default: {script_dir}/../.cache/html. "
                             "Stabilne sesje (>7 dni temu) używają cache. '' aby wyłączyć.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Wyłącz cache HTML.")
    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    print("=== Radoskop Scraper: Rada Miasta Szczecina (BIP) ===")
    print(f"Backend: requests + BeautifulSoup")
    print()

    init_session()

    # Cache HTML — default w cities/szczecin/.cache/html/ (w gitignore)
    if args.no_cache:
        cache_dir = None
    elif args.cache_dir is not None:
        cache_dir = args.cache_dir or None
    else:
        cache_dir = str(Path(__file__).resolve().parent.parent / ".cache" / "html")
    init_cache(cache_dir)
    if cache_dir:
        print(f"Cache HTML: {cache_dir}\n")

    # 1. Session list
    print("[1/3] Pobieranie listy sesji...")
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
        print("\n--- Tabele na stronie sesji ---")
        for i, table in enumerate(soup.find_all("table")[:3]):
            print(f"  [{i}] {len(table.find_all('tr'))} wierszy")
            headers = table.find_all("tr")[0].find_all(["th", "td"])
            for h in headers[:5]:
                print(f"      {h.get_text(strip=True)[:40]}")
        return

    # 2. Fetch votes for each session via eSesja
    print(f"\n[2/3] Pobieranie głosowań z eSesja ({len(all_sessions)} sesji)...")
    esesja_map = fetch_esesja_session_map()

    all_votes = []
    for si, session in enumerate(all_sessions):
        print(f"\n  Sesja {session['number']} ({session['date']}) [{si+1}/{len(all_sessions)}]")
        esesja_url = esesja_map.get(session["date"])
        if not esesja_url:
            print(f"    Brak sesji w eSesja dla daty {session['date']}")
            continue
        votes = scrape_session_votes(session, esesja_url)
        for v in votes:
            all_votes.append(v)

    print(f"  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        sys.exit(1)

    # 3. Build output
    print(f"\n[3/3] Budowanie pliku wyjściowego...")
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
    build_profiles_json(output, args.profiles)


if __name__ == "__main__":
    main()
