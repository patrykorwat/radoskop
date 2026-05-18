#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Gdyni.

Źródło: https://bip.um.gdynia.pl/ (Biuletyn Informacji Publicznej)
BIP Gdyni to React SPA — korzystamy z Playwright do renderowania strony
i pobierania protokołów PDF. Głosowania imienne parsujemy z PDF-ów (PyMuPDF).

Podejście:
  1. Playwright → załaduj stronę listy sesji IX kadencji
  2. Znajdź linki do protokołów PDF (tekst "Protokół X sesji")
  3. Pobierz i sparsuj każdy PDF → wyniki głosowań imiennych
  4. Zbuduj data.json w formacie Radoskop

Użycie:
    pip install playwright pymupdf requests
    playwright install chromium
    python scrape_gdynia.py [--output docs/data.json] [--profiles docs/profiles.json]
                            [--dry-run] [--max-sessions 5]

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje bip.um.gdynia.pl
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

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Zainstaluj: pip install pymupdf")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BIP_BASE = "https://bip.um.gdynia.pl"
BIP_SESSIONS_IX = f"{BIP_BASE}/sesje-rady,1702/sesje-rady-ix-kadencji,596553"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 1.0
PDF_DIR = None  # Set in main()
KNOWN_COUNCILORS: set[str] = set()  # Populated from profiles.json in main()
COUNCILOR_CANONICAL: dict[str, str] = {}  # normalized → canonical name

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


def _normalize_name(name: str) -> str:
    """Normalize a name for matching: lowercase, collapse spaces, strip hyphens."""
    n = name.lower().strip()
    n = re.sub(r'\s*-\s*', '-', n)   # "Śrubarczyk - Cichowska" → "śrubarczyk-cichowska"
    n = re.sub(r'\s+', ' ', n)
    return n


def _is_known_councilor(name: str) -> bool:
    """Check if a parsed name matches any known councilor (fuzzy on whitespace/hyphens)."""
    if not KNOWN_COUNCILORS:
        return True  # No profiles loaded — accept all (backward compat)
    norm = _normalize_name(name)
    return norm in KNOWN_COUNCILORS


def load_known_councilors(profiles_path: str) -> tuple[set[str], dict[str, str]]:
    """Load normalized councilor names from profiles.json for validation.

    Returns (set of normalized names, dict of normalized→canonical name).
    """
    path = Path(profiles_path)
    if not path.exists():
        return set(), {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    names = set()
    canonical = {}
    for p in data.get("profiles", []):
        norm = _normalize_name(p["name"])
        names.add(norm)
        canonical[norm] = p["name"]
    return names, canonical


def parse_polish_date(text: str) -> str | None:
    """Parse '25 lutego 2026' or '25 lutego 2026 r.' → '2026-02-25'."""
    text = text.strip().rstrip(".")
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
# Step 1: Get session list + protocol PDFs from the single listing page
# ---------------------------------------------------------------------------

def scrape_session_list_playwright() -> list[dict]:
    """Load BIP session listing page, find all protocol PDF links.

    The page at BIP_SESSIONS_IX contains a table with rows per session.
    Each row has a link like "Protokół III sesji" pointing to a .pdf file.
    We grab those links along with session dates and numbers.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Zainstaluj: pip install playwright && playwright install chromium")
        sys.exit(1)

    sessions = []

    print(f"  Ładuję {BIP_SESSIONS_IX}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="pl-PL",
        )
        page = context.new_page()
        page.goto(BIP_SESSIONS_IX, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(4000)

        # Get all links on the page with their parent row context
        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(el => ({
                href: el.href,
                text: el.textContent.trim(),
                rowText: el.closest('tr') ? el.closest('tr').textContent.trim() : ''
            }))"""
        )

        print(f"  Znaleziono {len(links)} linków na stronie")

        # Find protocol PDF links: text contains "Protokół" and href points to a file download
        # NOTE: legacy.um.gdynia.pl URLs may be truncated (ending .pd instead of .pdf)
        date_pattern = re.compile(
            r'(\d{1,2})[\.\s]+(stycznia|lutego|marca|kwietnia|maja|czerwca|'
            r'lipca|sierpnia|września|października|listopada|grudnia|\d{2})[\.\s]+(\d{4})',
            re.IGNORECASE
        )
        session_num_pattern = re.compile(r'([IVXLCDM]+)\s+[Ss]esj', re.IGNORECASE)

        for link in links:
            href = link["href"]
            text = link["text"]
            row_text = link["rowText"]

            # Must have "Protokół" in the link text
            if not any(w in text.lower() for w in ["protokol", "protokół", "protok"]):
                continue
            # Must look like a file download (PDF or legacy hash-based URL)
            href_lower = href.lower()
            is_file = (".pdf" in href_lower or ".pd" in href_lower
                       or "downloadfile" in href_lower or "/hash/" in href_lower)
            if not is_file:
                continue

            # Extract date from surrounding row text or link text
            # Dates can be "14 stycznia 2026" or "14.01.2026"
            context_text = text + " " + row_text
            date_m = date_pattern.search(context_text)
            if not date_m:
                continue

            day = date_m.group(1).lstrip("0") or "0"
            month_part = date_m.group(2)
            year = date_m.group(3)

            if month_part.isdigit():
                # Numeric date: "14.01.2026"
                month = int(month_part)
                date = f"{year}-{month:02d}-{int(day):02d}"
            else:
                # Polish date: "14 stycznia 2026"
                date_str = f"{day} {month_part} {year}"
                date = parse_polish_date(date_str)

            if not date or date < "2024-05-07":
                continue

            # Extract session number (Roman numeral)
            num_m = session_num_pattern.search(context_text)
            number = num_m.group(1) if num_m else "?"

            sessions.append({
                "number": number,
                "date": date,
                "url": BIP_SESSIONS_IX,
                "pdf_url": href,
            })
            print(f"    Sesja {number} ({date}) → {href.split('/')[-1][:60]}")

        browser.close()

    print(f"\n  Znaleziono {len(sessions)} sesji z protokołem PDF")
    return sorted(sessions, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Download and parse protocol PDFs
# ---------------------------------------------------------------------------

def download_pdf(url: str, session_date: str) -> Path | None:
    """Download a PDF protocol to local cache."""
    global PDF_DIR
    if PDF_DIR is None:
        return None

    filename = f"protokol_{session_date}.pdf"
    filepath = PDF_DIR / filename

    if filepath.exists() and filepath.stat().st_size > 1000:
        print(f"    (cache) {filename}")
        return filepath

    try:
        # Fix truncated URLs (.pd → .pdf)
        if url.endswith('.pd') and '.pdf' not in url:
            url = url + 'f'

        time.sleep(DELAY)
        print(f"    Pobieranie {url.split('/')[-1][:60]}...")
        resp = requests.get(url, timeout=120, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })
        resp.raise_for_status()

        if len(resp.content) < 500:
            print(f"    UWAGA: PDF za mały ({len(resp.content)} B) — pomijam")
            return None

        filepath.write_bytes(resp.content)
        print(f"    Zapisano: {filename} ({len(resp.content) / 1024:.0f} KB)")
        return filepath
    except Exception as e:
        print(f"    BŁĄD pobierania: {e}")
        return None


def parse_protocol_pdf(pdf_path: Path, session: dict) -> list[dict]:
    """Parse a BIP Gdynia protocol PDF to extract named votes.

    Protocol format (from sample XXVI session PDF):
      Głosowano w sprawie: [topic]
      Wyniki głosowania
      ZA: X, PRZECIW: Y, WSTRZYMUJĘ SIĘ: Z, BRAK GŁOSU: W, NIEOBECNI: N
      Wyniki imienne:
      ZA (N) Name1, Name2, ...
      PRZECIW (N) Name1, Name2, ...
      WSTRZYMUJĘ SIĘ (N) Name1, Name2, ...
      BRAK GŁOSU (N) Name1, Name2, ...
      NIEOBECNI (N) Name1, Name2, ...

      Uchwała została podjęta: X/Y/Z
    """
    doc = fitz.open(str(pdf_path))
    full_text = ""
    for page in doc:
        full_text += page.get_text() + "\n"
    doc.close()

    # Remove page headers/footers that break up text
    # Format A: "XXVI Sesja Rady Miasta Gdyni z dnia 14 stycznia 2026r.  \nStrona25"
    full_text = re.sub(
        r'\n?\s*[IVXLCDM]+\s+Sesja\s+Rady\s+Miasta\s+Gdyni\s+z\s+dnia\s+\d{1,2}\s+\w+\s+\d{4}r?\.\s*\n\s*Strona\s*\d+\s*\n?',
        ' ',
        full_text,
        flags=re.IGNORECASE
    )
    # Format B: "XV sesja RADY MIASTA GDYNI – 5 marca 2025 r." or with page number on next line
    full_text = re.sub(
        r'\n\d+\s*\n\s*[IVXLCDM]+\s+sesja\s+RADY\s+MIASTA\s+GDYNI\s+.{5,40}\d{4}\s*r?\.\s*\n?',
        '\n',
        full_text,
        flags=re.IGNORECASE
    )
    full_text = re.sub(
        r'\n[IVXLCDM]+\s+sesja\s+RADY\s+MIASTA\s+GDYNI\s+.{5,40}\d{4}\s*r?\.\s*\n',
        '\n',
        full_text,
        flags=re.IGNORECASE
    )
    # Also remove standalone "Strona X" lines
    full_text = re.sub(r'\nStrona\s*\d+\s*\n', '\n', full_text)

    votes = []

    # Try multiple split strategies based on protocol format.
    #
    # Format A/B: "Głosowano [wniosek] w sprawie: [topic]\nZA (N) ..."
    # Format C:   "[topic]\nWyniki głosowania ZA: N, PRZECIW: M, ...\nZA (N) ..."
    #
    # We try "Głosowano w sprawie:" first; if that finds nothing,
    # fall back to splitting on "Wyniki głosowania" (which precedes the ZA: line).

    vote_sections = re.split(
        r'Głosowano\s+(?:wniosek\s+)?w\s+sprawie:\s*',
        full_text,
        flags=re.IGNORECASE
    )

    if len(vote_sections) > 1:
        # Format A/B — "Głosowano w sprawie:" found
        for vi, section in enumerate(vote_sections[1:], 1):
            vote = _parse_vote_section(section, session, vi)
            if vote:
                votes.append(vote)
    else:
        # Format C — split on "Wyniki głosowania" and grab topic from text before
        vote_matches = list(re.finditer(
            r'Wyniki\s+głosowania\s+ZA:\s*(\d+)',
            full_text,
            flags=re.IGNORECASE
        ))
        if not vote_matches:
            print(f"    Brak głosowań w {pdf_path.name}")
            return []

        for vi, match in enumerate(vote_matches, 1):
            # Section goes from this match to the next (or end of text)
            start = match.start()
            end = vote_matches[vi].start() if vi < len(vote_matches) else len(full_text)
            section = full_text[start:end]

            # Extract topic from text BEFORE this vote marker
            # Look backwards for discussion context (up to 500 chars)
            pre_text = full_text[max(0, start - 500):start]
            vote = _parse_vote_section_format_c(section, pre_text, session, vi)
            if vote:
                votes.append(vote)

    return votes


def _parse_vote_section(section_text: str, session: dict, vote_idx: int) -> dict | None:
    """Parse a single vote section from the protocol PDF text.

    Supports two formats:
      Format A (XXVI session style):
        [topic]
        Wyniki głosowania
        ZA: 26, PRZECIW: 0, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 0, NIEOBECNI: 2
        Wyniki imienne:
        ZA (26) Name1, Name2, ...

      Format B (XV session style — no "Wyniki głosowania"/"Wyniki imienne" headers):
        [topic]
        ZA (28) Name1, Name2, ...
        PRZECIW (0)
        WSTRZYMUJĘ SIĘ (0)
    """

    # --- Topic: everything before first "ZA (" or "Wyniki głosowania" ---
    first_za = re.search(r'ZA\s*\(\d+\)', section_text)
    wyniki_match = re.search(r'Wyniki\s+głosowania', section_text, re.IGNORECASE)

    # Pick the earliest marker as topic boundary
    topic_end = len(section_text)
    if wyniki_match:
        topic_end = min(topic_end, wyniki_match.start())
    if first_za:
        topic_end = min(topic_end, first_za.start())

    topic_raw = section_text[:topic_end].strip()
    topic = re.sub(r'\s+', ' ', topic_raw).strip()
    topic = topic[:500] if topic else f"Głosowanie {vote_idx}"

    # --- Find the named vote region ---
    # Look for "Wyniki imienne:" header first (Format A)
    imienne_match = re.search(r'Wyniki\s+imienne:?\s*', section_text, re.IGNORECASE)

    if imienne_match:
        names_text = section_text[imienne_match.end():]
    elif first_za:
        # Format B: names start directly at "ZA (N)"
        names_text = section_text[first_za.start():]
    else:
        return None

    # Cut at end markers (next vote, discussion, etc.)
    end_markers = [
        r'Głosowano\s+(?:wniosek\s+)?w\s+sprawie:',
        r'Uchwała\s+została\s+podjęta',
        r'Uchwała\s+nie\s+została',
        r'Ad\s+\d+\.',
        r'Przewodniczący\s+\w',
        r'\nAd\s+',
        r'\n[A-ZĄĆĘŁŃÓŚŹŻ]\.\s*[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+[\.\-]',
        r'\nRadn[aey]\s+\w',
        r'\nWiceprzewodnicz',
        r'\nPrezydent\s',
        r'\nKomisja\s',
        r'\n\s*\n\s*\n',
    ]
    for marker in end_markers:
        end_m = re.search(marker, names_text, re.IGNORECASE)
        if end_m:
            names_text = names_text[:end_m.start()]

    # --- Parse named votes ---
    named_votes = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
    _parse_named_category(names_text, r'ZA\s*\((\d+)\)', "za", named_votes)
    _parse_named_category(names_text, r'PRZECIW\s*\((\d+)\)', "przeciw", named_votes)
    _parse_named_category(names_text, r'WSTRZYMUJĘ?\s+SIĘ\s*\((\d+)\)', "wstrzymal_sie", named_votes)
    _parse_named_category(names_text, r'BRAK\s+GŁOSU\s*\((\d+)\)', "brak_glosu", named_votes)
    _parse_named_category(names_text, r'NIEOBECNI\s*\((\d+)\)', "nieobecni", named_votes)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        return None

    # --- Vote counts: prefer explicit "ZA: N" line, fall back to parsed names ---
    counts = {}
    za_m = re.search(r'ZA:\s*(\d+)', section_text)
    counts["za"] = int(za_m.group(1)) if za_m else len(named_votes["za"])
    przeciw_m = re.search(r'PRZECIW:\s*(\d+)', section_text)
    counts["przeciw"] = int(przeciw_m.group(1)) if przeciw_m else len(named_votes["przeciw"])
    wstrzymal_m = re.search(r'WSTRZYMUJĘ\s+SIĘ:\s*(\d+)', section_text, re.IGNORECASE)
    counts["wstrzymal_sie"] = int(wstrzymal_m.group(1)) if wstrzymal_m else len(named_votes["wstrzymal_sie"])
    brak_m = re.search(r'BRAK\s+GŁOSU:\s*(\d+)', section_text, re.IGNORECASE)
    counts["brak_glosu"] = int(brak_m.group(1)) if brak_m else len(named_votes["brak_glosu"])
    nieobecni_m = re.search(r'NIEOBECNI:\s*(\d+)', section_text, re.IGNORECASE)
    counts["nieobecni"] = int(nieobecni_m.group(1)) if nieobecni_m else len(named_votes["nieobecni"])

    # Deduplicate names that appear in multiple categories (PDF text bleed)
    _deduplicate_named_votes(named_votes, counts)

    # --- Resolution status ---
    resolution = None
    if re.search(r'Uchwała\s+została\s+podjęta', section_text, re.IGNORECASE):
        resolution = "przyjęta"
    elif re.search(r'Uchwała\s+nie\s+została\s+podjęta', section_text, re.IGNORECASE):
        resolution = "odrzucona"
    # Check for explicit vote ratios like "podjęta: 26/0/0"
    ratio_m = re.search(r'podjęta:\s*(\d+)/(\d+)/(\d+)', section_text)
    if ratio_m:
        resolution = "przyjęta"

    # Session number in vote_id prevents collisions when two sessions share
    # a date (Gdynia 2024-10-02 case).
    session_num = session.get("number") or ""
    num_part = f"_{session_num}" if session_num else ""
    vote_id = f"{session['date']}{num_part}_{vote_idx:03d}"

    return {
        "id": vote_id,
        "source_url": session.get("url", ""),
        "session_date": session["date"],
        "session_number": session["number"],
        "topic": topic,
        "druk": None,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


def _parse_vote_section_format_c(section_text: str, pre_text: str,
                                 session: dict, vote_idx: int) -> dict | None:
    """Parse Format C vote: 'Wyniki głosowania ZA: N, PRZECIW: M, ...\nZA (N) Name1, ...'

    Topic is extracted from the text before the vote marker (pre_text).
    """
    # --- Topic: extract from pre_text ---
    # Look for patterns like "6.1 udzielenia Prezydentowi..." or "Głosowanie uchwały:"
    # Take the last meaningful line(s) before the vote
    lines = [l.strip() for l in pre_text.strip().split('\n') if l.strip()]

    # Find topic: last line(s) that look like an agenda item or discussion conclusion
    topic = f"Głosowanie {vote_idx}"
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        # Skip short filler lines
        if len(line) < 10:
            continue
        # Skip lines that are just speaker attributions
        if re.match(r'^[A-ZĄĆĘŁŃÓŚŹŻ]\.\s*[A-Z]', line):
            continue
        if re.match(r'^Głosowanie[:\s]', line, re.IGNORECASE):
            continue
        if re.match(r'^Opinia\s+Komisji', line, re.IGNORECASE):
            continue
        # Found a topic line
        topic = re.sub(r'\s+', ' ', line).strip()[:500]
        break

    # --- Counts from "ZA: N, PRZECIW: M, ..." ---
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    za_m = re.search(r'ZA:\s*(\d+)', section_text)
    if za_m:
        counts["za"] = int(za_m.group(1))
    przeciw_m = re.search(r'PRZECIW:\s*(\d+)', section_text)
    if przeciw_m:
        counts["przeciw"] = int(przeciw_m.group(1))
    wstrzymal_m = re.search(r'WSTRZYMUJĘ\s+SIĘ:\s*(\d+)', section_text, re.IGNORECASE)
    if wstrzymal_m:
        counts["wstrzymal_sie"] = int(wstrzymal_m.group(1))
    brak_m = re.search(r'BRAK\s+GŁOSU:\s*(\d+)', section_text, re.IGNORECASE)
    if brak_m:
        counts["brak_glosu"] = int(brak_m.group(1))
    nieobecni_m = re.search(r'NIEOBECNI:\s*(\d+)', section_text, re.IGNORECASE)
    if nieobecni_m:
        counts["nieobecni"] = int(nieobecni_m.group(1))

    # --- Named votes ---
    named_votes = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}

    # Find where ZA (N) starts
    first_za = re.search(r'ZA\s*\(\d+\)', section_text)
    if not first_za:
        return None

    names_text = section_text[first_za.start():]

    # Cut at end markers
    end_markers = [
        r'Głosowano\s+(?:wniosek\s+)?w\s+sprawie:',
        r'Uchwała\s+została\s+podjęta',
        r'Uchwała\s+nie\s+została',
        r'Ad\s+\d+\.',
        r'Przewodniczący\s+\w',
        r'\nAd\s+',
        r'\n[A-ZĄĆĘŁŃÓŚŹŻ]\.\s*[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+[\.\-]',
        r'\nRadn[aey]\s+\w',
        r'\nWiceprzewodnicz',
        r'\nPrezydent\s',
        r'\nKomisja\s',
        r'\n\s*\n\s*\n',
    ]
    for marker in end_markers:
        end_m = re.search(marker, names_text, re.IGNORECASE)
        if end_m:
            names_text = names_text[:end_m.start()]

    _parse_named_category(names_text, r'ZA\s*\((\d+)\)', "za", named_votes)
    _parse_named_category(names_text, r'PRZECIW\s*\((\d+)\)', "przeciw", named_votes)
    _parse_named_category(names_text, r'WSTRZYMUJĘ?\s+SIĘ\s*\((\d+)\)', "wstrzymal_sie", named_votes)
    _parse_named_category(names_text, r'BRAK\s+GŁOSU\s*\((\d+)\)', "brak_glosu", named_votes)
    _parse_named_category(names_text, r'NIEOBECNI\s*\((\d+)\)', "nieobecni", named_votes)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        return None

    # Use parsed name counts as fallback
    for cat in counts:
        if counts[cat] == 0 and len(named_votes[cat]) > 0:
            counts[cat] = len(named_votes[cat])

    # Deduplicate names that appear in multiple categories (PDF text bleed)
    _deduplicate_named_votes(named_votes, counts)

    # --- Resolution status ---
    resolution = None
    if re.search(r'Uchwała\s+została\s+podjęta', section_text, re.IGNORECASE):
        resolution = "przyjęta"
    elif re.search(r'Uchwała\s+nie\s+została\s+podjęta', section_text, re.IGNORECASE):
        resolution = "odrzucona"

    # Session number in vote_id prevents collisions when two sessions share
    # a date (Gdynia 2024-10-02 case).
    session_num = session.get("number") or ""
    num_part = f"_{session_num}" if session_num else ""
    vote_id = f"{session['date']}{num_part}_{vote_idx:03d}"

    return {
        "id": vote_id,
        "source_url": session.get("url", ""),
        "session_date": session["date"],
        "session_number": session["number"],
        "topic": topic,
        "druk": None,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


def _deduplicate_named_votes(named_votes: dict, counts: dict):
    """Remove names that appear in multiple vote categories.

    PDF text sometimes bleeds between vote sections, causing the same
    name to appear in e.g. both 'za' and 'przeciw'.  We trust the
    explicit counts (from the "ZA: N, PRZECIW: M" line) and keep names
    in the category whose parsed-name count is closest to its expected
    count, removing from the other.  Within each category the *first*
    N names (by parse order) are kept.
    """
    # Build a mapping: name -> list of categories it appears in
    name_cats: dict[str, list[str]] = {}
    for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]:
        for name in named_votes.get(cat, []):
            name_cats.setdefault(name, []).append(cat)

    duplicates = {n: cats for n, cats in name_cats.items() if len(cats) > 1}
    if not duplicates:
        return

    for name, cats in duplicates.items():
        # Keep name in the category whose explicit count is > 0 and
        # where the name is within the first count[cat] entries.
        keep_cat = None
        for cat in cats:
            expected = counts.get(cat, 0)
            if expected > 0:
                idx = named_votes[cat].index(name)
                if idx < expected:
                    keep_cat = cat
                    break
        if not keep_cat:
            # Fallback: keep in the first category encountered
            keep_cat = cats[0]

        for cat in cats:
            if cat != keep_cat:
                named_votes[cat] = [n for n in named_votes[cat] if n != name]

    # Trim each category to its expected count (if known)
    for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]:
        expected = counts.get(cat, 0)
        if expected > 0 and len(named_votes[cat]) > expected:
            named_votes[cat] = named_votes[cat][:expected]


def _parse_named_category(text: str, pattern: str, category: str, named_votes: dict):
    """Parse a named vote category from the 'Wyniki imienne' section.

    Example: 'ZA (26) Norbert Anisowicz, Dominik Aziewicz, ...'
    The names continue until the next category marker.
    """
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return

    count = int(m.group(1))
    if count == 0:
        return

    # Get text after this category marker until the next category
    start = m.end()
    remaining = text[start:]

    # Find where the next category starts
    # Note: no \b word boundary — PDF text sometimes lacks space before category
    # markers (e.g. "ZastawnaPRZECIW (3)"), so \b would fail to match.
    next_patterns = [
        r'PRZECIW\s*\(\d+\)',
        r'WSTRZYMUJĘ?\s+SIĘ\s*\(\d+\)',
        r'BRAK\s+GŁOSU\s*\(\d+\)',
        r'NIEOBECNI\s*\(\d+\)',
        r'ZA\s*\(\d+\)',
    ]

    end_pos = len(remaining)
    for np_pattern in next_patterns:
        np_m = re.search(np_pattern, remaining, re.IGNORECASE)
        if np_m:
            end_pos = min(end_pos, np_m.start())

    names_text = remaining[:end_pos].strip()

    # Clean and split names
    # Names are comma-separated, possibly across lines
    names_text = re.sub(r'\s+', ' ', names_text)
    names_text = names_text.strip().rstrip(',')

    if not names_text:
        return

    # Split by commas
    raw_names = [n.strip() for n in names_text.split(',')]

    # Clean each name and validate against known councilors
    clean_names = []
    for name in raw_names:
        name = name.strip()
        # Remove leading/trailing punctuation
        name = name.strip('.,;:')
        # Must have at least first and last name
        if name and ' ' in name and len(name) > 3:
            # Normalize whitespace
            name = ' '.join(name.split())
            # Validate against known councilor list
            if _is_known_councilor(name):
                # Use canonical name from profiles if available
                canonical = COUNCILOR_CANONICAL.get(_normalize_name(name), name)
                clean_names.append(canonical)

    named_votes[category] = clean_names


# ---------------------------------------------------------------------------
# Step 3: Build output structures
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
    for club, cnts in club_votes.items():
        best = max(cnts, key=cnts.get)
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


def _make_slug(name: str) -> str:
    """Generate a URL-friendly slug from a Polish name."""
    import unicodedata
    # Normalize Polish chars
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    slug = name.lower()
    for src, dst in replacements.items():
        slug = slug.replace(src, dst)
    # Replace spaces and non-alphanumeric with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def merge_profiles(profiles_path: str, councilors: list[dict], kid: str):
    """Merge voting stats from scraped councilors back into profiles.json.

    The profile page reads all data from profiles.json kadencje entries.
    This function enriches each councilor's kadencja with:
      slug, has_voting_data, frekwencja, aktywnosc, zgodnosc_z_klubem,
      votes_*, rebellion_count, rebellions, etc.
    """
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path} — nie mogę zapisać profili")
        return

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # Build lookup: name → councilor stats
    stats_by_name = {c["name"]: c for c in councilors}

    for profile in data.get("profiles", []):
        name = profile["name"]
        # Add slug
        if "slug" not in profile:
            profile["slug"] = _make_slug(name)

        # Merge voting stats into kadencja
        if kid in profile.get("kadencje", {}):
            kd = profile["kadencje"][kid]
            stats = stats_by_name.get(name)
            if stats:
                kd["has_voting_data"] = True
                kd["frekwencja"] = stats["frekwencja"]
                kd["aktywnosc"] = stats["aktywnosc"]
                kd["zgodnosc_z_klubem"] = stats["zgodnosc_z_klubem"]
                kd["votes_total"] = stats["votes_total"]
                kd["votes_za"] = stats["votes_za"]
                kd["votes_przeciw"] = stats["votes_przeciw"]
                kd["votes_wstrzymal"] = stats["votes_wstrzymal"]
                kd["votes_brak"] = stats["votes_brak"]
                kd["votes_nieobecny"] = stats["votes_nieobecny"]
                kd["rebellion_count"] = stats["rebellion_count"]
                kd["rebellions"] = stats["rebellions"]
                kd["has_activity_data"] = stats.get("has_activity_data", False)
                kd["activity"] = stats.get("activity")
            else:
                kd["has_voting_data"] = False

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  Zaktualizowano {profiles_path} ({len(data.get('profiles', []))} profili)")


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
    votes_by_date = defaultdict(list)
    for v in all_votes:
        votes_by_date[v["session_date"]].append(v)

    result = []
    seen_numbers = set()
    for s in sessions_raw:
        number = s.get("number", "")
        if number in seen_numbers:
            continue  # skip duplicate sessions
        seen_numbers.add(number)

        date = s["date"]
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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Gdyni (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--pdf-dir", default="scripts/pdfs", help="Katalog na pobrane PDF-y")
    parser.add_argument("--pdf-only", default=None, help="Parsuj jeden PDF (ścieżka) bez Playwright")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Usuń pobrane PDF-y z cache przed scrape")
    args = parser.parse_args()

    global DELAY, PDF_DIR, KNOWN_COUNCILORS, COUNCILOR_CANONICAL
    DELAY = args.delay
    PDF_DIR = Path(args.pdf_dir)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_COUNCILORS, COUNCILOR_CANONICAL = load_known_councilors(args.profiles)
    if KNOWN_COUNCILORS:
        print(f"  Załadowano {len(KNOWN_COUNCILORS)} znanych radnych do walidacji imion")

    if args.clear_cache:
        import glob
        for f in glob.glob(str(PDF_DIR / "*.pdf")):
            Path(f).unlink()
        print(f"  Wyczyszczono cache PDF: {PDF_DIR}")


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

    print("=== Radoskop Scraper: Rada Miasta Gdyni (BIP) ===")
    print(f"Źródło: {BIP_BASE}")
    print(f"Backend: Playwright + PyMuPDF")
    print()

    # --- Single PDF mode (for testing) ---
    if args.pdf_only:
        pdf_path = Path(args.pdf_only)
        if not pdf_path.exists():
            print(f"BŁĄD: Plik {pdf_path} nie istnieje")
            sys.exit(1)

        # Extract session info from filename or PDF content
        date = None
        date_m = re.search(r'(\d{2})(\d{2})(\d{4})', pdf_path.name)
        if date_m:
            dd, mm, yyyy = date_m.group(1), date_m.group(2), date_m.group(3)
            if 1 <= int(mm) <= 12 and 2020 <= int(yyyy) <= 2030:
                date = f"{yyyy}-{mm}-{dd}"

        if not date:
            doc = fitz.open(str(pdf_path))
            first_page = doc[0].get_text() if doc.page_count > 0 else ""
            doc.close()
            dm = re.search(r'(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|'
                           r'lipca|sierpnia|września|października|listopada|grudnia)\s+(\d{4})',
                           first_page, re.IGNORECASE)
            if dm:
                date = parse_polish_date(dm.group(0))

        if not date:
            date = "2026-01-01"

        number = "?"
        doc = fitz.open(str(pdf_path))
        first_page = doc[0].get_text() if doc.page_count > 0 else ""
        doc.close()
        num_m = re.search(r'([IVXLCDM]+)\s+sesji', first_page, re.IGNORECASE)
        if num_m:
            number = num_m.group(1)

        session = {"number": number, "date": date, "url": ""}
        votes = parse_protocol_pdf(pdf_path, session)
        print(f"\nSparsowano {len(votes)} głosowań z {pdf_path.name}")
        for v in votes:
            total = sum(len(nv) for nv in v["named_votes"].values())
            print(f"  {v['id']}: {v['topic'][:80]}")
            print(f"    ZA:{v['counts']['za']} PRZECIW:{v['counts']['przeciw']} "
                  f"WSTRZ:{v['counts']['wstrzymal_sie']} BRAK:{v['counts']['brak_glosu']} "
                  f"NIEOB:{v['counts']['nieobecni']} (imienne: {total})")
        return

    # --- Full scrape mode ---

    # 1. Session list
    print(f"[1/3] Pobieranie listy sesji z BIP...")
    all_sessions_raw = scrape_session_list_playwright()
    # Deduplicate sessions by number (BIP can list same session twice)
    seen_nums = set()
    all_sessions = []
    for s in all_sessions_raw:
        if s["number"] not in seen_nums:
            seen_nums.add(s["number"])
            all_sessions.append(s)

    if not all_sessions:
        print("BŁĄD: Nie znaleziono sesji z protokołem PDF.")
        print(f"Sprawdź ręcznie: {BIP_SESSIONS_IX}")
        sys.exit(1)

    if args.max_sessions > 0:
        all_sessions = all_sessions[:args.max_sessions]
        print(f"  (ograniczono do {args.max_sessions} sesji)")

    if args.dry_run:
        print("\nZnalezione sesje:")
        for s in all_sessions:
            print(f"  {s['number']:>8} | {s['date']} | {s['pdf_url'].split('/')[-1][:60]}")
        return

    # 2. Download and parse PDFs
    print(f"\n[2/3] Pobieranie i parsowanie protokołów PDF ({len(all_sessions)} sesji)...")
    all_votes = []

    for si, session in enumerate(all_sessions):
        print(f"\n  Sesja {session['number']} ({session['date']}) [{si+1}/{len(all_sessions)}]")

        pdf_path = download_pdf(session["pdf_url"], session["date"])
        if not pdf_path:
            print(f"    Nie udało się pobrać PDF — pomijam")
            continue

        votes = parse_protocol_pdf(pdf_path, session)
        print(f"    Sparsowano {len(votes)} głosowań")
        all_votes.extend(votes)

    print(f"\n  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań w żadnym protokole.")
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

    # Merge voting stats back into profiles.json for profile page rendering
    merge_profiles(args.profiles, councilors, kid)

    print(f"\nGotowe! Zapisano do {out_path}")
    total_v = len(all_votes)
    named_v = sum(1 for v in all_votes if sum(len(nv) for nv in v["named_votes"].values()) > 0)
    print(f"  {len(sessions_data)} sesji, {total_v} głosowań ({named_v} z imiennymi), {len(councilors)} radnych")



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
