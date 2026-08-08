#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Poznania.

Źródło: bip.poznan.pl — JSON API
BIP Poznań udostępnia publiczne JSON API bez autoryzacji.

Struktura BIP API:
  1. Archiwum sesji: https://bip.poznan.pl/api-json/bip/sesje/archiwum/
     — zwraca listę sesji ze slug + id
  2. Sesja (JSON): https://bip.poznan.pl/api-json/bip/sesje/{slug},{id}/
     — sesja_program.items[] → program[].zalaczniki → PDF-y głosowań
  3. Załącznik (PDF): https://bip.poznan.pl/bip/attachments.html?co=show&id={id}
     — format eSesja: nagłówek z tematem + tabela "Lp. / Nazwisko / Głos"
     — Głosy: ZA, PRZECIW, WSTRZYMUJĘ SIĘ, NIEOBECNY/NIEOBECNA

Użycie:
    pip install requests pymupdf
    python scrape_poznan.py [--output docs/data.json] [--profiles docs/profiles.json]
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

BIP_BASE = "https://bip.poznan.pl/"
API_BASE = "https://bip.poznan.pl/api-json/"
SESSIONS_ARCHIVE_URL = f"{API_BASE}bip/sesje/archiwum/"
ATTACHMENT_URL = "https://bip.poznan.pl/public/bip/attachments.att?co=show&instance=1097&parent={parent}&lang=pl&id={id}"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 1.0

# Radni Poznania IX kadencja (34 radnych)
# Nazwiska w formacie "Imię Nazwisko" -- jak w profiles.json
# Źródło: config.json → club_assignments (jedno źródło prawdy)
# Zweryfikowano: 2026-08-08 (BIP Poznań — profile indywidualne radnych)
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


def fetch_json(url: str) -> dict:
    """Fetch a JSON API endpoint and return parsed dict."""
    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def swap_name_order(name: str) -> str:
    """Convert 'Lastname Firstname' (PDF) to 'Firstname Lastname' (profiles).

    eSesja PDFs use 'Nazwisko Imię' format. The last word is always the first
    name (imię), everything before is the last name (nazwisko).
    Examples:
      'Alexandrowicz Przemysław' → 'Przemysław Alexandrowicz'
      'Bonk-Hammermeister Dorota' → 'Dorota Bonk-Hammermeister'
      'Szynkowska vel Sęk Sara' → 'Sara Szynkowska vel Sęk'
    """
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[-1]} {' '.join(parts[:-1])}"
    return name


def make_name_key(name: str) -> tuple:
    """Create a normalized key for fuzzy name matching.

    Sorts lowercased parts so 'Jan Kowalski' == 'Kowalski Jan'.
    """
    return tuple(sorted(name.lower().split()))


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

def scrape_session_list() -> list[dict]:
    """Fetch the session list from BIP Poznań JSON API.

    API endpoint: https://bip.poznan.pl/api-json/bip/sesje/archiwum/
    Returns sessions with id, numer (roman), date, and kadencja.
    """
    data = fetch_json(SESSIONS_ARCHIVE_URL)

    sessions = []
    root = data.get("bip.poznan.pl", {}).get("data", [])

    for entry in root:
        sesje_data = entry.get("sesje", {})
        items = sesje_data.get("items", [])

        for item in items:
            # items can be {"sesja": {...}} or {"sesja": [{...}]}
            sesja_list = item.get("sesja", {})
            if isinstance(sesja_list, dict):
                sesja_list = [sesja_list]

            for sesja in sesja_list:
                sesja_id = sesja.get("id")
                numer = sesja.get("numer", "").strip()
                date_str = sesja.get("sesja", "")  # "2025-01-17 09:00"
                kadencja = sesja.get("kadencja")

                if not sesja_id or not numer:
                    continue

                # Parse date from "2025-01-17 09:00"
                date = date_str[:10] if len(date_str) >= 10 else None
                if not date:
                    continue

                # Build slug for API URL (lowercase roman numeral)
                slug = numer.lower()
                api_url = f"{API_BASE}bip/sesje/{slug},{sesja_id}/"
                html_url = f"{BIP_BASE}bip/sesje/{slug},{sesja_id}/"

                sessions.append({
                    "id": sesja_id,
                    "number": numer,
                    "date": date,
                    "url": html_url,
                    "api_url": api_url,
                    "kadencja": kadencja,
                })

    # Deduplicate by id
    seen = set()
    unique = []
    for s in sessions:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)

    # Filter by kadencja -- only sessions from 2024-05-07 onwards
    kadencja_start = KADENCJE["2024-2029"]["start"]
    filtered = [s for s in unique if s["date"] >= kadencja_start]
    print(f"  Znaleziono {len(unique)} sesji ogółem, {len(filtered)} w kadencji 2024-2029")

    if not filtered and unique:
        print(f"  UWAGA: Brak sesji po {kadencja_start}. Najnowsza: {unique[-1]['date']}")
        return sorted(unique, key=lambda x: x["date"])

    return sorted(filtered, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape session page → find PDF links
# ---------------------------------------------------------------------------

def scrape_session_pdf_links(session: dict) -> list[dict]:
    """Fetch session JSON API and find all vote PDF attachment links.

    Each PDF on BIP Poznań is one vote result (eSesja system).
    We look for attachments with "Głosowanie" in the description.
    """
    api_url = session.get("api_url")
    if not api_url:
        slug = session["number"].lower()
        api_url = f"{API_BASE}bip/sesje/{slug},{session['id']}/"

    data = fetch_json(api_url)

    pdf_links = []
    root = data.get("bip.poznan.pl", {}).get("data", [])

    for entry in root:
        program = entry.get("sesja_program", {})
        items = program.get("items", [])

        for item in items:
            prog_list = item.get("program", [])
            if isinstance(prog_list, dict):
                prog_list = [prog_list]

            for prog in prog_list:
                # Get topic from opisy
                topic = ""
                opisy = prog.get("opisy", {}).get("items", [])
                for opis_item in opisy:
                    opis = opis_item.get("opis", {})
                    if isinstance(opis, dict):
                        dc_text = opis.get("dc_text", "")
                        # Strip HTML tags
                        clean = re.sub(r'<[^>]+>', '', dc_text).strip()
                        if clean:
                            topic = clean

                # Get attachments
                zal_data = prog.get("zalaczniki", {})
                zal_items = zal_data.get("items", [])

                for zal_item in zal_items:
                    if not isinstance(zal_item, dict):
                        continue

                    # Can be {"zalacznik": {...}} or {"zalacznik": [{...}]}
                    zal = zal_item.get("zalacznik", {})
                    if isinstance(zal, dict):
                        zal = [zal]
                    if isinstance(zal, list):
                        for z in zal:
                            if not isinstance(z, dict):
                                continue
                            opis = z.get("opis", "")
                            mime = z.get("mime", "")
                            att_id = z.get("id")
                            att_parent = z.get("parent", "")

                            # Filter: only voting PDFs
                            is_vote = "łosowani" in opis  # Głosowanie
                            is_pdf = "pdf" in mime.lower()

                            if is_vote and is_pdf and att_id:
                                url = ATTACHMENT_URL.format(id=att_id, parent=att_parent)
                                pdf_links.append({
                                    "url": url,
                                    "text": opis,
                                    "att_id": str(att_id),
                                    "topic_context": topic,
                                })

    # Deduplicate by att_id
    seen = set()
    unique_links = []
    for pl in pdf_links:
        if pl["att_id"] not in seen:
            seen.add(pl["att_id"])
            unique_links.append(pl)

    return unique_links


# ---------------------------------------------------------------------------
# Step 3: Download and parse PDF
# ---------------------------------------------------------------------------

def download_pdf(pdf_url: str, cache_dir: Path) -> Path | None:
    """Download a PDF from URL to cache directory."""
    # Extract attachment ID from URL (co=show&id=NNNNN)
    m_id = re.search(r'[?&]id=(\d+)', pdf_url)
    m_att = re.search(r'/attachments/download/(\d+)', pdf_url)
    m_pdf = re.search(r'/([^/]+\.pdf)', pdf_url)

    if m_id:
        filename = f"glosowanie_{m_id.group(1)}.pdf"
    elif m_att:
        filename = f"glosowanie_{m_att.group(1)}.pdf"
    elif m_pdf:
        filename = m_pdf.group(1)
    else:
        # Use hash of URL
        import hashlib
        h = hashlib.md5(pdf_url.encode()).hexdigest()[:12]
        filename = f"vote_{h}.pdf"

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


def parse_vote_from_pdf(pdf_path: Path) -> list[dict]:
    """Parse a single vote PDF from BIP Poznań.

    Each PDF is ONE vote with this structure (ESESJA system):
        N. Temat głosowania
        X Sesja Rady Miasta Poznania
        Głosowanie
        1
        Typ głosowania  jawne
        Data głosowania:  DD.MM.YYYY HH:MM
        Liczba uprawnionych  34
        Głosy za  N
        Liczba obecnych  N
        Głosy przeciw  N
        Liczba nieobecnych  N
        Głosy wstrzymujące się  N
        Obecni niegłosujący  N
        ...
        Uprawnieni do głosowania
        Lp  Nazwisko i imię  Głos  Lp.  Nazwisko i imię  Głos
        1.  Imię Nazwisko  ZA  ...

    Returns list with 0 or 1 vote dicts.
    """
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

    lines = full_text.split('\n')

    # Check if this is a vote PDF
    if not any("Głosy za" in l or "Uprawnieni do głosowania" in l for l in lines):
        return votes

    # --- Extract topic ---
    topic = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^\d+\.\s+', line):
            topic = re.sub(r'^\d+\.\s+', '', line).strip()
            break
        if len(line) > 10 and "Sesja" not in line and "Głosowanie" != line:
            topic = line
            break
    if not topic:
        topic = "Głosowanie"

    # --- Extract vote counts from header ---
    counts = {
        "za": 0,
        "przeciw": 0,
        "wstrzymal_sie": 0,
        "brak_glosu": 0,
        "nieobecni": 0,
    }

    for i, line in enumerate(lines):
        ls = line.strip()
        if ls == "Głosy za" and i + 1 < len(lines):
            try: counts["za"] = int(lines[i + 1].strip())
            except ValueError: pass
        elif ls == "Głosy przeciw" and i + 1 < len(lines):
            try: counts["przeciw"] = int(lines[i + 1].strip())
            except ValueError: pass
        elif ls.startswith("Głosy wstrzymujące") and i + 1 < len(lines):
            try: counts["wstrzymal_sie"] = int(lines[i + 1].strip())
            except ValueError: pass
        elif ls == "Liczba nieobecnych" and i + 1 < len(lines):
            try: counts["nieobecni"] = int(lines[i + 1].strip())
            except ValueError: pass
        elif ls == "Obecni niegłosujący" and i + 1 < len(lines):
            try: counts["brak_glosu"] = int(lines[i + 1].strip())
            except ValueError: pass

    # --- Extract individual votes from table ---
    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    # Find start of vote table
    table_start = None
    for i, line in enumerate(lines):
        if "Uprawnieni do głosowania" in line:
            table_start = i + 1
            break
    if table_start is None:
        for i, line in enumerate(lines):
            if "Nazwisko i imię" in line:
                table_start = i + 1
                break
    if table_start is None:
        return votes

    # Skip header lines
    while table_start < len(lines):
        l = lines[table_start].strip()
        if l in ("Lp", "Lp.", "Nazwisko i imię", "Głos", ""):
            table_start += 1
        else:
            break

    # Collect table lines
    table_lines = []
    for line in lines[table_start:]:
        l = line.strip()
        if not l:
            continue
        if l.startswith("Wydrukowano:"):
            break
        table_lines.append(l)

    # Parse using regex — handles two-column PDF layout where PyMuPDF
    # merges columns into one line, e.g.:
    #   "1 Alexandrowicz Przemysław WSTRZYMUJE SIĘ 18 Matuszak Paweł ZA"
    VOTE_RE = re.compile(
        r'(\d+)\.?\s+'                  # Lp (ordinal number)
        r'([^0-9]+?)\s+'                # Name (no digits, non-greedy)
        r'(ZA|PRZECIW'
        r'|WSTRZYMUJ[EĘ]\s+SI[ĘE]'
        r'|WSTRZYMA[ŁL]A?\s+SI[ĘE]'
        r'|NIEOBECN[YA]'
        r'|NIE\s+G[ŁL]OSOWA[ŁL]A?'
        r'|OBECN[YA]'
        r'|NIEODDANY'
        r')(?=\s+\d|\s*$)',             # followed by next entry or end
        re.IGNORECASE
    )

    # Join all table lines into one string for regex matching
    table_text = ' '.join(table_lines)
    matches = VOTE_RE.findall(table_text)

    for _lp, raw_name, vote_str in matches:
        name = re.sub(r'\s+', ' ', raw_name).strip()
        name = re.sub(r'-\s+', '-', name)
        if not name or len(name) < 3:
            continue
        # PDF uses "Nazwisko Imię" — swap to "Imię Nazwisko"
        name = swap_name_order(name)

        vote_upper = vote_str.upper().strip()
        if vote_upper == "ZA":
            named_votes["za"].append(name)
        elif vote_upper == "PRZECIW":
            named_votes["przeciw"].append(name)
        elif "WSTRZYMUJ" in vote_upper or "WSTRZYMA" in vote_upper:
            named_votes["wstrzymal_sie"].append(name)
        elif "NIE" in vote_upper and "GŁOSOWA" in vote_upper:
            named_votes["brak_glosu"].append(name)
        elif "NIEOBECN" in vote_upper:
            named_votes["nieobecni"].append(name)
        elif "OBECN" in vote_upper:
            named_votes["brak_glosu"].append(name)
        elif vote_upper == "NIEODDANY":
            named_votes["brak_glosu"].append(name)

    # Deduplicate
    for cat in named_votes:
        named_votes[cat] = list(dict.fromkeys(named_votes[cat]))

    total_named = sum(len(v) for v in named_votes.values())
    if total_named > 0:
        votes.append({
            "topic": topic[:500],
            "counts": counts,
            "named_votes": named_votes,
        })

    return votes


# ---------------------------------------------------------------------------
# Step 4: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping.

    Returns dict keyed by both original name AND name_key for fuzzy matching.
    """
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path} — kluby będą oznaczone jako '?'")
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = {}
    key_lookup = {}  # make_name_key → profile dict
    for p in data.get("profiles", []):
        name = p["name"]
        kadencje = p.get("kadencje", {})
        if kadencje:
            latest = list(kadencje.values())[-1]
            profile = {
                "name": name,
                "club": latest.get("club", "?"),
                "district": latest.get("okręg"),
            }
            result[name] = profile
            key_lookup[make_name_key(name)] = profile

    # Also add COUNCILORS hardcoded dict entries — nadpisują profiles.json
    for cname, club in COUNCILORS.items():
        profile = {"name": cname, "club": club, "district": None}
        result[cname] = profile
        key_lookup[make_name_key(cname)] = profile

    # Store key_lookup for use in matching
    result["__key_lookup__"] = key_lookup
    return result


def lookup_profile(name: str, profiles: dict) -> dict:
    """Look up a councilor profile by name, trying exact match then key match."""
    # Exact match
    if name in profiles and isinstance(profiles[name], dict) and "club" in profiles[name]:
        return profiles[name]
    # Key-based fuzzy match (handles name order differences)
    key_lookup = profiles.get("__key_lookup__", {})
    key = make_name_key(name)
    if key in key_lookup:
        return key_lookup[key]
    return {"name": name, "club": "?", "district": None}


def compute_club_majority(vote: dict, profiles: dict) -> dict[str, str]:
    """For each club, compute the majority position in a given vote."""
    club_votes = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    for cat in ["za", "przeciw", "wstrzymal_sie"]:
        for name in vote["named_votes"].get(cat, []):
            club = lookup_profile(name, profiles).get("club", "?")
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
        prof = lookup_profile(name, profiles)
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


def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Poznania (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--max-pdfs", type=int, default=0, help="Maks. PDF na sesję (0=wszystkie, do testów)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--explore", action="store_true", help="Pobierz 1 sesję i pokaż strukturę")
    parser.add_argument("--all-kadencje", action="store_true", help="Nie filtruj po kadencji")
    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    print("=== Radoskop Scraper: Rada Miasta Poznania (BIP) ===")
    print(f"Backend: requests + PyMuPDF (JSON API)")
    print()

    init_session()

    total_steps = 3

    # 1. Session list
    print(f"[1/{total_steps}] Pobieranie listy sesji...")
    all_sessions = scrape_session_list()

    if args.all_kadencje:
        print(f"  --all-kadencje: nie filtruję po dacie kadencji")

    if not all_sessions:
        print("BŁĄD: Nie znaleziono sesji.")
        print(f"Sprawdź ręcznie: {SESSIONS_ARCHIVE_URL}")
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

        # Use JSON API to find PDF attachments
        pdf_links = scrape_session_pdf_links(s0)

        print(f"\n--- Załączniki głosowań ({len(pdf_links)}) ---")
        for pl in pdf_links[:10]:
            print(f"  [{pl['text'][:80]}] -> {pl['url']}")
        if len(pdf_links) > 10:
            print(f"  ... i {len(pdf_links)-10} więcej")

        # Try downloading and parsing first PDF
        if pdf_links:
            cache = Path("pdfs")
            cache.mkdir(exist_ok=True)
            pdf_path = download_pdf(pdf_links[0]["url"], cache)
            if pdf_path:
                result = parse_vote_from_pdf(pdf_path)
                if result:
                    v = result[0]
                    total = sum(len(nv) for nv in v["named_votes"].values())
                    print(f"\n--- Próba parsowania 1. PDF ---")
                    print(f"  Temat: {v['topic'][:80]}")
                    print(f"  Głosy: za={v['counts']['za']}, przeciw={v['counts']['przeciw']}, wstrz={v['counts']['wstrzymal_sie']}")
                    print(f"  Imiennych: {total}")
                else:
                    print(f"\n--- Nie sparsowano głosowania z 1. PDF ---")
                    import fitz as _fitz
                    d = _fitz.open(str(pdf_path))
                    txt = d[0].get_text()[:500]
                    d.close()
                    print(f"  Tekst PDF:\n{txt}")
        return

    # 2. Fetch PDFs and parse votes for each session
    print(f"\n[2/{total_steps}] Pobieranie protokołów i głosowań ({len(all_sessions)} sesji)...")
    all_votes = []
    cache_dir = Path("pdfs")
    cache_dir.mkdir(parents=True, exist_ok=True)

    for si, session in enumerate(all_sessions):
        print(f"\n  Sesja {session['number']} ({session['date']}) [{si+1}/{len(all_sessions)}]")

        pdf_links = scrape_session_pdf_links(session)
        print(f"    Znaleziono {len(pdf_links)} linków do głosowań (PDF)")

        if args.max_pdfs > 0:
            pdf_links = pdf_links[:args.max_pdfs]
            print(f"    (ograniczono do {args.max_pdfs} PDF)")

        for pi, pdf_link in enumerate(pdf_links):
            pdf_path = download_pdf(pdf_link["url"], cache_dir)
            if not pdf_path:
                continue

            votes_from_pdf = parse_vote_from_pdf(pdf_path)
            print(f"    Sparsowano {len(votes_from_pdf)} głosowań z PDF")

            for vi, vote_data in enumerate(votes_from_pdf):
                vote_id = f"{session['date']}_{pi:03d}_{vi:03d}"
                vote = {
                    "id": vote_id,
                    "source_url": pdf_link["url"],
                    "session_date": session["date"],
                    "session_number": session["number"],
                    "topic": vote_data["topic"],
                    "druk": None,
                    "resolution": None,
                    "counts": vote_data["counts"],
                    "named_votes": vote_data["named_votes"],
                }
                all_votes.append(vote)

    print(f"\n  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        sys.exit(1)

    # 3. Build output
    print(f"\n[3/{total_steps}] Budowanie pliku wyjściowego...")

    # Load or use hardcoded profiles
    profiles = load_profiles(args.profiles)
    if not profiles:
        # Use hardcoded COUNCILORS if no profiles.json
        profiles = {name: {"name": name, "club": club, "district": None}
                   for name, club in COUNCILORS.items()}
        print(f"  Załadowano profile: {len(profiles)} radnych (z listy hardcoded)")
    else:
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
    merge_stats_to_profiles(args.profiles, output)



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
