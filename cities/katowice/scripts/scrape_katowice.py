#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Katowice.

Źródło: BIP Katowice (bip.katowice.eu)
Głosowania imienne Rady Miasta publikowane sa jako PDF na stronach sesji.

Struktura BIP:
  1. Lista sesji: https://bip.katowice.eu/RadaMiasta/Sesje/default.aspx?menu=658
     Linki do sesji: sesja.aspx?idt=XXX&menu=658
  2. Strona sesji: sesja.aspx?idt=XXX
     Link "IMIENNE WYNIKI GŁOSOWAŃ" -> dokument.aspx?idr=YYY
  3. Strona dokumentu: dokument.aspx?idr=YYY
     Linki do PDF: /SiteAssets/.../Sesja NN, Glosowanie M, Data ....pdf
  4. Kazdy PDF zawiera: temat glosowania, liste radnych, glos kazdego radnego

UWAGA: Uruchom lokalnie.

Użycie:
    pip install requests beautifulsoup4 pdfplumber
    python scrape_katowice.py [--output docs/data.json] [--profiles docs/profiles.json]
"""

import argparse
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)

try:
    import pdfplumber
except ImportError:
    print("Zainstaluj: pip install pdfplumber")
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


# ============================================================================
# Config
# ============================================================================

BIP_BASE = "https://bip.katowice.eu"
BIP_SESSIONS = f"{BIP_BASE}/RadaMiasta/Sesje/default.aspx?menu=658"

KADENCJE = {
    "2024-2029": {
        "label": "IX kadencja (2024\u20132029)",
        "start": "2024-05-07",
    }
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://katowice.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,application/xhtml+xml",
}

DELAY = 1.0

# Sesje starsze niż tyle dni traktujemy jako stabilne. BIP nie aktualizuje
# protokołów po tym czasie. Dla takich sesji pomijamy GET session.aspx + GET
# dokument.aspx (zaoszczędzone 2 HTTP × 150 sesji = ~300 requestów i ~5 min).
STABLE_AGE_DAYS = 7

# Wersja schematu state.json. Bump gdy zmieni się shape pdf_links albo
# pola wymagane do skipu HTTP. Stary cache po bumpie zostanie pominięty.
STATE_VERSION = 1

# Placeholder for councilors with clubs — fill from profiles.json or manual list
COUNCILORS = {
    # KO (Koalicja Obywatelska) - 14 members
    "Beata Bala": "KO",
    "Patryk Białas": "KO",
    "Łukasz Borkowski": "KO",
    "Elżbieta Grodzka-Łopuszyńska": "KO",
    "Urszula Machowska": "KO",
    "Jarosław Makowski": "KO",
    "Tomasz Maśnica": "KO",
    "Ewa Sadkowska": "KO",
    "Magdalena Skwarek": "KO",
    "Adam Szymczyk": "KO",
    "Andrzej Warmuz": "KO",
    "Barbara Wnęk-Gabor": "KO",
    "Magdalena Wieczorek": "KO",
    "Adam Lejman-Gąska": "KO",
    # PiS (Prawo i Sprawiedliwość) - 5 members
    "Leszek Piechota": "PiS",
    "Krystyna Panek": "PiS",
    "Piotr Pietrasz": "PiS",
    "Mariusz Skiba": "PiS",
    "Piotr Trząski": "PiS",
    # Forum Samorządowe i Marcin Krupa - 9 members
    "Agnieszka Piątek": "Forum",
    "Jacek Kalisz": "Forum",
    "Maciej Biskupski": "Forum",
    "Łukasz Hankus": "Forum",
    "Dawid Kamiński": "Forum",
    "Barbara Mańdok": "Forum",
    "Borys Pronobis": "Forum",
    "Maria Ryś": "Forum",
    "Adam Skowron": "Forum",
    "Krzysztof Pieczyński": "Forum",
    "Roch Sobula": "Forum",
    "Damian Stępień": "Forum",
    "Krzysztof Kraus": "Forum",
}


# Build flexible name lookup: eSesja uses "Lastname Firstname"
# while COUNCILORS uses "Firstname Lastname".
def _build_name_lookup(councilors: dict[str, str]) -> dict[str, str]:
    lookup = {}
    for name, club in councilors.items():
        lookup[name] = club
        parts = name.split()
        if len(parts) >= 2:
            lookup[f"{parts[-1]} {' '.join(parts[:-1])}"] = club
            lookup[f"{parts[-1]} {parts[0]}"] = club
    return lookup

_CLUB_LOOKUP = _build_name_lookup(COUNCILORS)


def resolve_club(name: str) -> str:
    """Resolve a councillor name (any format) to their club."""
    if name in _CLUB_LOOKUP:
        return _CLUB_LOOKUP[name]
    parts = name.split()
    if parts:
        last = parts[0]
        for key, club in _CLUB_LOOKUP.items():
            if key.split()[0] == last or key.split()[-1] == last:
                return club
    return "?"


# Build canonical name lookup: maps any name variant to "Firstname Lastname" form.
def _build_canonical_lookup(councilors: dict[str, str]) -> dict[str, str]:
    lookup = {}
    for name in councilors:
        lookup[name] = name  # Firstname Lastname -> itself
        parts = name.split()
        if len(parts) >= 2:
            # Lastname Firstname -> Firstname Lastname
            reversed_name = f"{parts[-1]} {' '.join(parts[:-1])}"
            lookup[reversed_name] = name
            # Also single-swap for multi-part names
            lookup[f"{parts[-1]} {parts[0]}"] = name
    return lookup


_CANONICAL = _build_canonical_lookup(COUNCILORS)


def normalize_name(name: str) -> str:
    """Normalize a councillor name to canonical Firstname Lastname form."""
    if name in _CANONICAL:
        return _CANONICAL[name]
    # Try case-insensitive match
    name_lower = name.lower()
    for key, canonical in _CANONICAL.items():
        if key.lower() == name_lower:
            return canonical
    # Unknown councillor: keep original form
    return name


# ============================================================================
# Step 1: Fetch session list from BIP Katowice
# ============================================================================

def fetch_soup(http_session, url):
    """GET a page and return BeautifulSoup."""
    resp = http_session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_session_list(http_session, debug=False):
    """Fetch council session list from BIP Katowice.

    BIP lists sessions as links: <a href="sesja.aspx?idt=XXX&menu=658">Sesja XXVI</a>
    with text "z dnia YYYY-MM-DD" nearby.
    We only take sessions from the current kadencja (2024-2029).
    """
    if debug:
        print(f"[DEBUG] GET {BIP_SESSIONS}")

    try:
        soup = fetch_soup(http_session, BIP_SESSIONS)
    except Exception as e:
        print(f"  Blad pobierania listy sesji: {e}")
        return []

    sessions = []
    page_text = soup.get_text()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "sesja.aspx" not in href or "idt=" not in href:
            continue

        text = a.get_text(strip=True)
        if not text.lower().startswith("sesja"):
            continue

        # Extract idt parameter
        idt_match = re.search(r'idt=(\d+)', href)
        if not idt_match:
            continue
        idt = idt_match.group(1)

        # Extract session number (roman)
        num_match = re.search(r'Sesja\s+([IVXLCDM]+)', text, re.IGNORECASE)
        number = num_match.group(1) if num_match else ""

        # Find date: look for "z dnia YYYY-MM-DD" in surrounding text
        parent = a.parent
        if not parent:
            continue
        parent_text = parent.get_text()
        date_match = re.search(r'z\s+dnia\s+(\d{4}-\d{2}-\d{2})', parent_text)
        if not date_match:
            # Try broader context
            grandparent = parent.parent
            if grandparent:
                gp_text = grandparent.get_text()
                date_match = re.search(r'z\s+dnia\s+(\d{4}-\d{2}-\d{2})', gp_text)
        if not date_match:
            continue

        date_str = date_match.group(1)
        session_url = href
        if not session_url.startswith("http"):
            session_url = f"{BIP_BASE}/RadaMiasta/Sesje/{session_url}"

        sessions.append({
            "date": date_str,
            "url": session_url,
            "idt": idt,
            "title": f"Sesja {number} ({date_str})",
            "number": number,
        })

    # Deduplicate by idt
    seen = set()
    unique = []
    for s in sessions:
        if s["idt"] not in seen:
            seen.add(s["idt"])
            unique.append(s)

    # Filter by kadencja start date
    kadencja_start = KADENCJE["2024-2029"]["start"]
    filtered = [s for s in unique if s["date"] >= kadencja_start]

    if debug:
        print(f"[DEBUG] {len(unique)} sesji ogolnie, {len(filtered)} w kadencji 2024-2029")

    return sorted(filtered, key=lambda x: x["date"])


# ============================================================================
# State cache: idt → pdf_links (skip session/doc page GETs for stable sessions)
# ============================================================================

def load_state(state_path):
    """Wczytuje state.json z cache'em listy PDF-ów per sesja.

    Plik ma kształt:
      {"version": 1, "discovered": {"<idt>": {date, pdf_links, ...}}}

    Niezgodna wersja schematu → traktujemy jako pusty cache (rebuilduje się
    przy pierwszym run-ie). Brak pliku też zwraca pusty słownik.
    """
    if not state_path:
        return {}
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != STATE_VERSION:
            return {}
        return data.get("discovered", {})
    except Exception:
        return {}


def save_state(state_path, discovered):
    """Zapisuje state.json. Bezpieczne na concurrent writes nie jest (single-writer)."""
    if not state_path:
        return
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STATE_VERSION, "discovered": discovered}
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(p)


def is_session_stable(session_date_str):
    """True gdy data sesji to co najmniej STABLE_AGE_DAYS dni temu.

    BIP Katowic publikuje finalne PDF-y w ciągu kilku dni od sesji.
    Po tygodniu zawartość nie zmienia się, więc nie ma sensu ponownie
    fetchować listy.
    """
    try:
        dt = datetime.strptime(session_date_str, "%Y-%m-%d")
    except ValueError:
        return False
    age = (datetime.now() - dt).days
    return age >= STABLE_AGE_DAYS


# ============================================================================
# Step 2: For each session, find voting PDFs and parse them
# ============================================================================

def discover_pdf_links(http_session, session_url, debug=False):
    """Hituje BIP żeby zebrać listę PDF-ów dla pojedynczej sesji.

    Robi 2 HTTP requesty: GET sesja.aspx (znajdź IMIENNE WYNIKI) plus GET
    dokument.aspx (zbierz PDF-y). Zwraca listę pdf_link dictów albo [].

    Caller decyduje czy w ogóle uruchamiać tę funkcję (state cache).
    """
    if debug:
        print(f"    [DEBUG] GET {session_url}")

    try:
        soup = fetch_soup(http_session, session_url)
    except Exception as e:
        print(f"    Blad pobierania sesji: {e}")
        return []

    # Find the "IMIENNE WYNIKI GŁOSOWAŃ" document link
    doc_url = None
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).upper()
        if "IMIENNE" in text and "WYNIK" in text and "GŁOS" in text:
            href = a["href"]
            if not href.startswith("http"):
                href = f"{BIP_BASE}/RadaMiasta/Sesje/{href}"
            doc_url = href
            break

    if not doc_url:
        if debug:
            print(f"    [DEBUG] Brak linku IMIENNE WYNIKI GLOSOWAN")
        return []

    time.sleep(DELAY * 0.3)

    if debug:
        print(f"    [DEBUG] GET {doc_url}")

    try:
        doc_soup = fetch_soup(http_session, doc_url)
    except Exception as e:
        print(f"    Blad pobierania dokumentu glosowan: {e}")
        return []

    pdf_links = []
    for a in doc_soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True)
        if not href.lower().endswith(".pdf"):
            continue
        if "glosowanie" not in link_text.lower() and "głosowanie" not in link_text.lower():
            if "SiteAssets" not in href and "Uprawnienia" not in href:
                continue

        full_url = href
        if href.startswith("/"):
            full_url = BIP_BASE + href

        vote_num_match = re.search(r'Glosowanie\s+(\d+)', link_text, re.IGNORECASE)
        vote_num = int(vote_num_match.group(1)) if vote_num_match else len(pdf_links) + 1

        pdf_links.append({
            "url": full_url,
            "vote_num": vote_num,
            "link_text": link_text,
        })

    pdf_links.sort(key=lambda x: x["vote_num"])

    if debug:
        print(f"    [DEBUG] {len(pdf_links)} PDF z glosowaniami")

    return pdf_links


def fetch_session_votes(http_session, session_info, debug=False, pdf_dir=None,
                       parsed_dir=None, state=None, force_rediscover=False):
    """Pobiera głosowania jednej sesji.

    Optymalizacja: state cache mapuje idt → pdf_links. Dla stabilnych sesji
    (starszych niż STABLE_AGE_DAYS) z niepustą listą cache'owanych PDF-ów
    pomijamy 2 HTTP requesty (session page + document page). To skraca warm
    cache run ze 150 sesji × ~2.3s sleep+HTTP ≈ 6 min do <1 min.

    state: dict {idt: {pdf_links, ...}} albo None (brak cache, zachowanie
    legacy). Modyfikowany in-place: po discovery dla nowej/recent sesji
    zapis wraca do caller'a przez referencję.
    """
    idt = session_info.get("idt", "")
    session_url = session_info["url"]

    pdf_links = None
    used_cache = False

    if state is not None and idt and not force_rediscover:
        cached = state.get(idt)
        if (cached
                and cached.get("pdf_links")
                and is_session_stable(session_info.get("date", ""))):
            pdf_links = cached["pdf_links"]
            used_cache = True
            if debug:
                print(f"    [DEBUG] STATE CACHE HIT idt={idt} ({len(pdf_links)} PDF), skip BIP")

    if pdf_links is None:
        pdf_links = discover_pdf_links(http_session, session_url, debug=debug)
        # Update state cache nawet dla niestabilnych sesji — następny run
        # może już złapać je jako stabilne.
        if state is not None and idt:
            state[idt] = {
                "date": session_info.get("date", ""),
                "number": session_info.get("number", ""),
                "title": session_info.get("title", ""),
                "url": session_url,
                "pdf_links": pdf_links,
                "discovered_at": datetime.now().isoformat(timespec="seconds"),
                "stable": is_session_stable(session_info.get("date", "")),
            }

    # Download and parse each PDF (cache layers obsługują brak network gdy warm)
    votes = []
    for pdf_info in pdf_links:
        vote = fetch_and_parse_vote_pdf(http_session, pdf_info, session_info, debug, pdf_dir=pdf_dir, parsed_dir=parsed_dir)
        if vote:
            votes.append(vote)
        parsed_file = _parsed_cache_path(pdf_info["url"], parsed_dir)
        if not (parsed_file and parsed_file.exists()):
            time.sleep(DELAY * 0.3)

    return votes


# ============================================================================
# PDF vote parsing
# ============================================================================

# Vote values as they appear in BIP Katowice PDFs.
# BIP uses mixed forms: "WSTRZYMUJĘ SIĘ" (1st person), "NIEOBECNA" (feminine), etc.
# OBECNY/OBECNA appear in attendance check votes and must be in the pattern
# so the regex correctly splits two-column entries, even though we skip them.
VOTE_PATTERN = re.compile(
    r'(\d+)\.\s+'
    r'(.+?)\s+'
    r'(ZA|PRZECIW|WSTRZYMUJ[ĘE] SI[ĘE]|WSTRZYMA[ŁL]A? SI[ĘE]|NIEOBECN[AY]|NIEODDANY|OBECN[AY])\b'
)

VOTE_MAP = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIE": "wstrzymal_sie",
    "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
    "WSTRZYMAŁA SIĘ": "wstrzymal_sie",
    "WSTRZYMAL SIE": "wstrzymal_sie",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
    "NIEODDANY": "brak_glosu",
    # OBECNY/OBECNA = present (attendance check). Mapped to None and skipped.
    "OBECNY": None,
    "OBECNA": None,
}


def _pdf_cache_path(url, pdf_dir):
    """Return cache file path for a PDF URL."""
    if not pdf_dir:
        return None
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', url.split('/')[-1])[:60]
    return Path(pdf_dir) / f"{h}_{safe}"


# Version bump this when parse_vote_text or VOTE_PATTERN changes in a way
# that should invalidate previously cached parsed results.
PARSED_CACHE_VERSION = 1


def _parsed_cache_path(url, parsed_dir):
    """Return cache file path for a parsed vote JSON."""
    if not parsed_dir:
        return None
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return Path(parsed_dir) / f"{h}.json"


def fetch_and_parse_vote_pdf(http_session, pdf_info, session_info, debug=False, pdf_dir=None, parsed_dir=None):
    """Download a single voting PDF and parse it.

    Two caching layers (both optional):
      1. parsed_dir: cached JSON with already-parsed {topic, counts, named_votes}.
         Cache HIT here skips BOTH download AND pdfplumber parsing (the expensive step).
      2. pdf_dir: cached raw PDF bytes. Cache HIT skips download only, pdfplumber
         still runs. Used when parsed cache is cold or invalid.
    """
    url = pdf_info["url"]
    parsed_file = _parsed_cache_path(url, parsed_dir)
    cache_file = _pdf_cache_path(url, pdf_dir)

    # Layer 1: parsed-vote cache (skips pdfplumber entirely)
    if parsed_file and parsed_file.exists() and parsed_file.stat().st_size > 0:
        try:
            with open(parsed_file, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("_version") == PARSED_CACHE_VERSION and "named_votes" in cached:
                if debug:
                    print(f"      [DEBUG] PARSED CACHE HIT {parsed_file.name}")
                return {
                    "topic": cached["topic"],
                    "counts": cached["counts"],
                    "named_votes": cached["named_votes"],
                }
            else:
                if debug:
                    print(f"      [DEBUG] PARSED CACHE version mismatch, re-parsing")
        except Exception as e:
            if debug:
                print(f"      [DEBUG] PARSED CACHE read error: {e}")

    # Layer 2: PDF byte cache (skips download, pdfplumber still runs)
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        if debug:
            print(f"      [DEBUG] PDF CACHE HIT {cache_file.name}")
        pdf_bytes = cache_file.read_bytes()
    else:
        if debug:
            print(f"      [DEBUG] GET {url}")
        try:
            resp = http_session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            if debug:
                print(f"      [DEBUG] Blad pobierania PDF: {e}")
            return None

        pdf_bytes = resp.content
        if b"%PDF" not in pdf_bytes[:10]:
            if debug:
                print(f"      [DEBUG] Odpowiedz nie jest PDF")
            return None

        # Save to disk cache
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(pdf_bytes)

    try:
        pdf_file = io.BytesIO(pdf_bytes)
        with pdfplumber.open(pdf_file) as pdf:
            if not pdf.pages:
                return None
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        if debug:
            print(f"      [DEBUG] Blad parsowania PDF: {e}")
        return None

    if not text:
        return None

    parsed = parse_vote_text(text, pdf_info, session_info, debug)

    # Write parsed-vote cache on success
    if parsed and parsed_file:
        try:
            parsed_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "_version": PARSED_CACHE_VERSION,
                "_url": url,
                "topic": parsed["topic"],
                "counts": parsed["counts"],
                "named_votes": parsed["named_votes"],
            }
            with open(parsed_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            if debug:
                print(f"      [DEBUG] PARSED CACHE write error: {e}")

    return parsed


def parse_vote_text(text, pdf_info, session_info, debug=False):
    """Parse the extracted text from a voting PDF.

    PDF structure:
      Line 1: "25 XXV sesja Rady Miasta Katowice"
      Line 2: "Głosowanie"
      Lines 3+: Vote number + subject (may wrap)
      Then: "Typ głosowania jawne Data głosowania: DD.MM.YYYY HH:MM"
      Then: counts (Liczba uprawnionych, Głosy za, etc.)
      Then: "Uprawnieni do głosowania"
      Then: two-column table of councillors with votes
      Last: "Wydrukowano: ..."
    """
    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    # Extract topic: everything between "Głosowanie" and "Typ głosowania"
    topic = ""
    topic_match = re.search(
        r'G[łl]osowanie\s*\n(.*?)Typ\s+g[łl]osowania',
        text, re.DOTALL | re.IGNORECASE
    )
    if topic_match:
        raw_topic = topic_match.group(1).strip()
        # Clean up: remove stray vote numbers at start, join lines
        lines = [l.strip() for l in raw_topic.split("\n") if l.strip()]
        topic = " ".join(lines)
        # Remove leading standalone number (vote number from PDF layout)
        topic = re.sub(r'^\d+\s+', '', topic)
        # Clean up multiple spaces
        topic = re.sub(r'\s{2,}', ' ', topic).strip()

    if not topic:
        topic = f"Glosowanie {pdf_info.get('vote_num', '?')}"

    # Extract councillor votes from the table section
    # Find text after "Uprawnieni do głosowania" and before "Wydrukowano"
    table_match = re.search(
        r'Uprawnieni\s+do\s+g[łl]osowania.*?\n(.*?)(?:Wydrukowano|$)',
        text, re.DOTALL | re.IGNORECASE
    )
    if table_match:
        table_text = table_match.group(1)
    else:
        # Fallback: try to find councillor entries anywhere in text
        table_text = text

    # Parse councillor entries: "1. Beata Bala ZA"
    for m in VOTE_PATTERN.finditer(table_text):
        name = m.group(2).strip()
        vote_val = m.group(3).upper()

        # Clean name: remove any trailing dots or numbers
        name = re.sub(r'\s*\d+\.$', '', name).strip()
        if not name or len(name) < 3:
            continue

        # Normalize to canonical Firstname Lastname form
        name = normalize_name(name)

        if vote_val in VOTE_MAP:
            cat = VOTE_MAP[vote_val]
            if cat is None:
                # OBECNY/OBECNA (attendance check): skip this entry
                continue
        elif "WSTRZYMA" in vote_val:
            cat = "wstrzymal_sie"
        else:
            continue

        named_votes[cat].append(name)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        if debug:
            print(f"      [DEBUG] Brak radnych w PDF (0 glosow)")
        return None

    counts = {cat: len(names) for cat, names in named_votes.items()}

    return {
        "topic": topic[:500],
        "counts": counts,
        "named_votes": named_votes,
    }


# ============================================================================
# Step 3: Build output structures
# ============================================================================

def load_profiles(profiles_path: str) -> dict:
    """Załaduj profiles.json z mapowaniem radny → klub."""
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
    """Dla każdego klubu oblicz główną pozycję w głosowaniu."""
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


def build_councilors(all_votes: list[dict], profiles: dict) -> list[dict]:
    """Zbuduj statystyki radnych z danych głosowań."""
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
            "votes_with_club": 0,
            "votes_against_club": 0,
            "rebellions": [],
        }

    for v in all_votes:
        club_majority = compute_club_majority(v, profiles)

        for name in v["named_votes"].get("za", []):
            if name in councilors:
                councilors[name]["votes_za"] += 1
                _check_rebellion(councilors[name], "za", club_majority, v)

        for name in v["named_votes"].get("przeciw", []):
            if name in councilors:
                councilors[name]["votes_przeciw"] += 1
                _check_rebellion(councilors[name], "przeciw", club_majority, v)

        for name in v["named_votes"].get("wstrzymal_sie", []):
            if name in councilors:
                councilors[name]["votes_wstrzymal"] += 1
                _check_rebellion(councilors[name], "wstrzymal_sie", club_majority, v)

        for name in v["named_votes"].get("brak_glosu", []):
            if name in councilors:
                councilors[name]["votes_brak"] += 1

        for name in v["named_votes"].get("nieobecni", []):
            if name in councilors:
                councilors[name]["votes_nieobecny"] += 1

    total_votes = len(all_votes)

    result = []
    for c in councilors.values():
        present_votes = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present_votes / total_votes * 100) if total_votes > 0 else 0
        total_club_votes = c["votes_with_club"] + c["votes_against_club"]
        zgodnosc = (c["votes_with_club"] / total_club_votes * 100) if total_club_votes > 0 else 0

        result.append({
            "name": c["name"],
            "club": c["club"],
            "district": c["district"],
            "frekwencja": 0.0,  # Na podstawie sesji, nie głosowań
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
    """Sprawdź, czy radny głosował inaczej niż większość klubu."""
    club = councilor["club"]
    if club == "?" or club not in club_majority:
        return

    majority_cat = club_majority[club]
    if vote_cat == majority_cat:
        councilor["votes_with_club"] += 1
    else:
        councilor["votes_against_club"] += 1
        councilor["rebellions"].append({
            "vote_id": vote.get("id", ""),
            "session": vote.get("session_date", ""),
            "topic": vote.get("topic", "")[:120],
            "their_vote": vote_cat,
            "club_majority": majority_cat,
        })


def compute_similarity(all_votes: list[dict], councilors_list: list[dict]) -> tuple[list, list]:
    """Oblicz pary radnych o najwyższym/najniższym podobieństwie głosowania."""
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


def build_sessions(all_votes: list[dict]) -> list[dict]:
    """Zbuduj dane sesji z liczby głosowań."""
    votes_by_date = defaultdict(list)
    for v in all_votes:
        date = v.get("session_date", "unknown")
        votes_by_date[date].append(v)

    result = []
    for date in sorted(votes_by_date.keys()):
        session_votes = votes_by_date[date]
        attendees = set()

        for v in session_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                attendees.update(v["named_votes"].get(cat, []))

        result.append({
            "date": date,
            "number": "",  # Brak numeru sesji w eSesja
            "vote_count": len(session_votes),
            "attendee_count": len(attendees),
            "attendees": sorted(attendees),
            "speakers": [],
        })

    return result


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


# ============================================================================
# Main
# ============================================================================

def scrape(output_path, profiles_path, debug=False, max_sessions=0, pdf_dir=None,
           parsed_dir=None, state_path=None, force_rediscover=False):
    """Glowna funkcja scrapowania.

    state_path: ścieżka do state.json z cache'em listy PDF-ów per sesja.
    Bez tego cache każdy run hituje BIP 2× per sesja nawet jeśli zawartość
    nie zmieniła się od miesięcy (95min → niezmiennie 95min).

    force_rediscover: ignoruj state cache, hituj BIP dla każdej sesji.
    Użyteczne po zmianach w parserze lub gdy podejrzewamy stale cache.
    """
    http_session = requests.Session()

    print("\n=== Radoskop Katowice  Scraper glosowan (BIP) ===")

    # Step 0: load incremental state (cache listy PDF-ów per idt sesji)
    state = load_state(state_path) if state_path else None
    if state is not None:
        print(f"  State cache: {len(state)} sesji w cache (z {state_path})")

    # Step 1: fetch session list from BIP
    print("\n[1/3] Pobieranie listy sesji...")
    session_list = fetch_session_list(http_session, debug=debug)

    if not session_list:
        print("BLAD: Nie znaleziono sesji")
        sys.exit(1)

    print(f"  Znaleziono: {len(session_list)} sesji")

    if max_sessions > 0:
        session_list = session_list[:max_sessions]

    # Step 2: for each session, fetch its vote list and then each vote detail
    print(f"\n[2/3] Pobieranie glosowan z {len(session_list)} sesji...")
    all_votes = []

    cache_hits = 0
    cache_misses = 0

    for i, session_info in enumerate(session_list):
        # Czy dla tej sesji skorzystamy ze state cache (krótki status pokaż
        # już przy nagłówku, żeby było widać scope kosztu w logu)
        idt = session_info.get("idt", "")
        will_use_cache = (
            state is not None and not force_rediscover and idt
            and state.get(idt, {}).get("pdf_links")
            and is_session_stable(session_info.get("date", ""))
        )
        marker = "(cache)" if will_use_cache else "(BIP)"
        print(f"  [{i+1}/{len(session_list)}] {session_info['date']}  {session_info.get('title', '')[:55]} {marker}")

        session_votes = fetch_session_votes(
            http_session, session_info, debug=debug,
            pdf_dir=pdf_dir, parsed_dir=parsed_dir,
            state=state, force_rediscover=force_rediscover,
        )

        if will_use_cache:
            cache_hits += 1
        else:
            cache_misses += 1

        for idx, vote_detail in enumerate(session_votes):
            vote_id = f"{session_info['date']}_{idx:03d}_000"
            all_votes.append({
                "id": vote_id,
                "source_url": vote_detail.get("source_url", session_info["url"]),
                "session_date": session_info["date"],
                "session_number": "",
                "topic": vote_detail.get("topic", ""),
                "druk": None,
                "resolution": None,
                "counts": vote_detail.get("counts", {}),
                "named_votes": vote_detail.get("named_votes", {}),
            })

        print(f"    {len(session_votes)} glosowan")
        # Sleep only after sesions where we hit BIP. Cache hits zostają natychmiastowe.
        if not will_use_cache:
            time.sleep(DELAY)

    # Save state cache po pełnym przejściu (atomic write)
    if state is not None and state_path:
        save_state(state_path, state)
        print(f"  Zapisano state: {len(state)} sesji do {state_path}")

    print(f"  Cache stats: {cache_hits} hits, {cache_misses} misses (BIP requests)")
    print(f"  Pobrano: {len(all_votes)} glosowan z danymi imiennymi")

    if not all_votes:
        print("BLAD: Brak glosowan z danymi")
        sys.exit(1)

    print(f"\n[3/3] Budowanie danych wyjsciowych...")
    profiles = load_profiles(profiles_path)

    # Always merge hardcoded COUNCILORS into profiles.
    # COUNCILORS is the authoritative source for club assignments.
    # profiles.json may exist but have incomplete or missing club data.
    hardcoded = {}
    for name, club in COUNCILORS.items():
        entry = {"name": name, "club": club, "district": None}
        hardcoded[name] = entry
        parts = name.split()
        if len(parts) >= 2:
            hardcoded[f"{parts[-1]} {' '.join(parts[:-1])}"] = entry
            hardcoded[f"{parts[-1]} {parts[0]}"] = entry

    if profiles:
        # Merge: hardcoded fills gaps, profiles.json entries with valid clubs take priority
        for key, entry in hardcoded.items():
            if key not in profiles:
                profiles[key] = entry
            elif profiles[key].get("club", "?") == "?":
                profiles[key]["club"] = entry["club"]
        print(f"  Załadowano profile: {len(profiles)} radnych (profiles.json + hardcoded)")
    else:
        profiles = hardcoded
        print(f"  Zaladowano profile: {len(COUNCILORS)} radnych (z listy hardcoded)")

    # Zbuduj struktury
    kid = "2024-2029"
    councilors = build_councilors(all_votes, profiles)
    sessions_data = build_sessions(all_votes)
    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"\n  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"  Kluby: {dict(club_counts)}")

    # Zbuduj output
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

    # Zapisz
    out_path = Path(output_path)
    save_split_output(output, out_path)

    print(f"\nGotowe! Zapisano do {out_path}")

    # Scal z profiles.json
    build_profiles_json(output, profiles_path)


def _default_project_dir():
    """Katalog projektu Katowic, względem lokalizacji skryptu.

    Skrypt jest w cities/katowice/scripts/, więc parent.parent = cities/katowice/.
    Cache'e i output trafiają domyślnie do tego katalogu, niezależnie skąd
    skrypt jest uruchamiany.
    """
    return Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(
        description="Scraper Rady Miasta Katowice (BIP). Cache PDF, parsed JSON "
                    "i state listy sesji są domyślnie włączone (w katalogu projektu)."
    )
    project_dir = _default_project_dir()
    parser.add_argument(
        "--output", default="docs/data.json",
        help="Plik wyjściowy (domyślnie: docs/data.json)"
    )
    parser.add_argument(
        "--profiles", default="docs/profiles.json",
        help="Plik profiles.json (domyślnie: docs/profiles.json)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Wlacz szczegolowe logowanie"
    )
    parser.add_argument(
        "--max-sessions", type=int, default=0,
        help="Maks. sesji (0=wszystkie)"
    )
    parser.add_argument(
        "--pdf-dir", default=str(project_dir / "pdfs"),
        help=f"Katalog cache PDF (pomija ponowne pobieranie). Default: {project_dir / 'pdfs'}. "
             "Ustaw '' (pusty) aby wyłączyć cache."
    )
    parser.add_argument(
        "--parsed-dir", default=str(project_dir / "cache" / "parsed"),
        help=f"Katalog cache sparsowanych glosowan JSON (pomija pdfplumber). "
             f"Default: {project_dir / 'cache' / 'parsed'}. Ustaw '' aby wyłączyć."
    )
    parser.add_argument(
        "--state-file", default=str(project_dir / "cache" / "state.json"),
        help="Plik state.json z cache'em listy PDF-ów per sesja. Skipuje GET "
             "sesja.aspx + GET dokument.aspx dla stabilnych sesji starszych "
             f"niż {STABLE_AGE_DAYS} dni. Default: {project_dir / 'cache' / 'state.json'}. "
             "Ustaw '' aby wyłączyć."
    )
    parser.add_argument(
        "--force-rediscover", action="store_true",
        help="Ignoruj state cache, hituj BIP dla każdej sesji (jak przed cache'em)."
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Wyłącz wszystkie cache'e (pdf-dir, parsed-dir, state-file)."
    )
    args = parser.parse_args()

    # --no-cache w jednym flagu wycina wszystko, dla testów cold path.
    if args.no_cache:
        pdf_dir = None
        parsed_dir = None
        state_file = None
    else:
        # Pusty string ('') jako wartość traktujemy jak None (jawne wyłączenie).
        pdf_dir = args.pdf_dir or None
        parsed_dir = args.parsed_dir or None
        state_file = args.state_file or None

    scrape(
        output_path=args.output,
        profiles_path=args.profiles,
        debug=args.debug,
        max_sessions=args.max_sessions,
        pdf_dir=pdf_dir,
        parsed_dir=parsed_dir,
        state_path=state_file,
        force_rediscover=args.force_rediscover,
    )


if __name__ == "__main__":
    main()
