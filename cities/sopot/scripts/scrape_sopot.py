#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Sopotu.

Źródło: https://bip.sopot.pl/ (Biuletyn Informacji Publicznej)
BIP Sopotu to React SPA — korzystamy z Playwright do renderowania strony
i pobierania załączników (PDF/DOCX) z wynikami głosowań.

Podejście:
  1. Playwright → załaduj stronę wyników głosowań kadencji 2024-2029
  2. Znajdź linki do plików załączników (raporty głosowań / wyniki głosowań)
  3. Pobierz pliki: PDF (sesje I–XVI) i DOCX (sesje XVII+)
  4. Sparsuj wyniki głosowań → data.json w formacie Radoskop

Użycie:
    pip install playwright pymupdf python-docx requests
    playwright install chromium
    python scrape_sopot.py [--output docs/data.json] [--profiles docs/profiles.json]
                           [--dry-run] [--max-sessions 5] [--cache-dir .cache]

UWAGA: Uruchom lokalnie — sandbox Cowork może blokować bip.sopot.pl
"""

import argparse
import json
import os
import re
import subprocess
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

BIP_BASE = "https://bip.sopot.pl"
BIP_VOTES_PAGE = f"{BIP_BASE}/a,23008,wyniki-glosowan-za-podjeciem-uchwal-kadencja-2024-2029.html"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

DELAY = 1.0
CACHE_DIR = None  # Set in main()
KNOWN_COUNCILORS: set[str] = set()
COUNCILOR_CANONICAL: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Roman numeral helpers
# ---------------------------------------------------------------------------

ROMAN_MAP = {
    'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000
}

def roman_to_int(s: str) -> int:
    """Convert Roman numeral string to integer."""
    result = 0
    prev = 0
    for ch in reversed(s.upper()):
        val = ROMAN_MAP.get(ch, 0)
        if val < prev:
            result -= val
        else:
            result += val
        prev = val
    return result

def int_to_roman(n: int) -> str:
    """Convert integer to Roman numeral string."""
    vals = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),
            (50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]
    result = ''
    for v, r in vals:
        while n >= v:
            result += r
            n -= v
    return result


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


# ---------------------------------------------------------------------------
# Name validation (same pattern as Gdynia scraper)
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Normalize a name for matching: lowercase, collapse spaces, strip hyphens."""
    n = name.lower().strip()
    n = re.sub(r'\s*-\s*', '-', n)
    n = re.sub(r'\s+', ' ', n)
    return n


def _is_known_councilor(name: str) -> bool:
    """Check if a parsed name matches any known councilor."""
    if not KNOWN_COUNCILORS:
        return True
    norm = _normalize_name(name)
    return norm in KNOWN_COUNCILORS


def load_known_councilors(profiles_path: str) -> tuple[set[str], dict[str, str]]:
    """Load normalized councilor names from profiles.json."""
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


# ---------------------------------------------------------------------------
# Step 1: Scrape attachment list from BIP voting results page
# ---------------------------------------------------------------------------

def scrape_attachment_list_playwright() -> list[dict]:
    """Load BIP voting results page, find all attachment download links.

    The page at BIP_VOTES_PAGE is a React SPA that renders a list of
    attachments (raporty głosowań / wyniki głosowań). Each attachment has:
    - A filename like "raporty głosowań I sesja RMS 7 maja 2024.pdf"
    - A download link like "bip.sopot.pl/e,pobierz,get.html?id=XXXXX"
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Zainstaluj: pip install playwright && playwright install chromium")
        sys.exit(1)

    attachments = []

    print(f"  Ładuję {BIP_VOTES_PAGE}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="pl-PL",
        )
        page = context.new_page()

        page.goto(BIP_VOTES_PAGE, wait_until="networkidle", timeout=30000)
        # Wait for React to render content
        page.wait_for_timeout(3000)

        # BIP Sopot renders attachments as <button> elements with:
        #   id="attachment-download-button-{numericId}"
        #   aria-label="Pobierz plik {filename}"
        # The download URL is: bip.sopot.pl/e,pobierz,get.html?id={numericId}
        buttons = page.query_selector_all('button[id^="attachment-download-button-"]')
        print(f"  Znaleziono {len(buttons)} przycisków pobierania")

        for btn in buttons:
            btn_id = btn.get_attribute("id") or ""
            aria_label = btn.get_attribute("aria-label") or ""

            # Extract numeric attachment ID from button id
            num_id = btn_id.replace("attachment-download-button-", "")
            if not num_id.isdigit():
                continue

            # Extract filename from aria-label: "Pobierz plik {filename}."
            filename = aria_label.replace("Pobierz plik ", "").rstrip(".")

            if not filename:
                continue

            # Build download URL
            download_url = f"{BIP_BASE}/e,pobierz,get.html?id={num_id}"

            # Determine format from filename
            is_docx = filename.lower().endswith(".docx")
            is_pdf = filename.lower().endswith(".pdf")

            # Some filenames lack extension (e.g. "raporty głosowań X sesja-30-01-2025")
            if not (is_pdf or is_docx):
                if "wyniki_głosowań" in filename.lower() or "wyniki_glosowan" in filename.lower():
                    is_docx = True
                else:
                    is_pdf = True  # Default: older sessions are PDFs

            # Extract session info from filename
            session_info = _parse_attachment_filename(filename)

            attachments.append({
                "filename": filename,
                "url": download_url,
                "attachment_id": num_id,
                "format": "docx" if is_docx else "pdf",
                **session_info,
            })

        browser.close()

    # Sort by session number
    attachments.sort(key=lambda x: x.get("session_num", 0))
    print(f"  Znaleziono {len(attachments)} załączników z głosowaniami")
    return attachments


def _parse_attachment_filename(filename: str) -> dict:
    """Extract session number and date from attachment filename.

    Examples:
        "raporty głosowań I sesja RMS 7 maja 2024.pdf" → {number: "I", date: "2024-05-07", session_num: 1}
        "raporty głosowań VIII_sesja_21-11-2024.pdf" → {number: "VIII", date: "2024-11-21", session_num: 8}
        "wyniki_głosowań_XVII_sesja_27_11_2025.docx" → {number: "XVII", date: "2025-11-27", session_num: 17}
        "raporty głosowań X sesja-30-01-2025" → {number: "X", date: "2025-01-30", session_num: 10}
    """
    result = {"number": "?", "date": None, "session_num": 0}

    # Clean up filename
    name = filename.strip()

    # Extract Roman numeral session number
    # Pattern: after "głosowań" or "głosowania", find Roman numeral
    roman_match = re.search(
        r'(?:głosowań|głosowania|raporty)\s+(?:głosowań\s+)?([IVXLCDM]+)\s*[_\s]?sesj',
        name, re.IGNORECASE
    )
    if not roman_match:
        # Try: "wyniki_głosowań_XVII_sesja"
        roman_match = re.search(r'[_\s]([IVXLCDM]+)[_\s]+sesj', name, re.IGNORECASE)
    if not roman_match:
        # Try: just find Roman numeral before "sesja" or "sesj"
        roman_match = re.search(r'([IVXLCDM]+)\s*[_\s-]*sesj', name, re.IGNORECASE)

    if roman_match:
        result["number"] = roman_match.group(1).upper()
        result["session_num"] = roman_to_int(result["number"])

    # Extract date — multiple patterns
    # Pattern 1: "7 maja 2024" (Polish date)
    polish_date = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', name)
    if polish_date:
        month = MONTHS_PL.get(polish_date.group(2).lower())
        if month:
            d = int(polish_date.group(1))
            y = int(polish_date.group(3))
            result["date"] = f"{y}-{month:02d}-{d:02d}"

    # Pattern 2: "21-11-2024" or "21_11_2024" or "29.10.2024" (DD-MM-YYYY)
    if not result["date"]:
        dmy = re.search(r'(\d{1,2})[_.\-](\d{1,2})[_.\-](\d{4})', name)
        if dmy:
            d, m, y = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
            if 1 <= m <= 12 and 1 <= d <= 31:
                result["date"] = f"{y}-{m:02d}-{d:02d}"

    # Pattern 3: "30-01-2025" at end after sesja-
    if not result["date"]:
        dmy = re.search(r'sesja[_\s-]+(\d{1,2})[_.\-](\d{1,2})[_.\-](\d{4})', name)
        if dmy:
            d, m, y = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
            if 1 <= m <= 12 and 1 <= d <= 31:
                result["date"] = f"{y}-{m:02d}-{d:02d}"

    return result


# ---------------------------------------------------------------------------
# Step 2: Download attachments
# ---------------------------------------------------------------------------

def download_attachment(att: dict) -> Path | None:
    """Download an attachment file (PDF or DOCX) to cache directory."""
    cache = Path(CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)

    ext = att["format"]
    safe_name = re.sub(r'[^\w\-.]', '_', att["filename"])
    if not safe_name.endswith(f".{ext}"):
        safe_name += f".{ext}"
    filepath = cache / safe_name

    if filepath.exists() and filepath.stat().st_size > 0:
        print(f"    [cache] {safe_name}")
        return filepath

    url = att["url"]
    print(f"    GET {url}")
    time.sleep(DELAY)

    try:
        resp = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        })
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            f.write(resp.content)

        print(f"    → {safe_name} ({len(resp.content) // 1024} KB)")
        return filepath
    except Exception as e:
        print(f"    BŁĄD pobierania: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 3: Parse voting data from files
# ---------------------------------------------------------------------------

def _reconcile_counts(votes: list[dict]) -> list[dict]:
    """Ensure vote counts match named_votes. OCR often garbles count text,
    but named_votes (parsed from individual rows) are reliable."""
    for v in votes:
        nv = v.get("named_votes", {})
        c = v.get("counts", {})

        named_za = len(nv.get("za", []))
        named_przeciw = len(nv.get("przeciw", []))
        named_wstrz = len(nv.get("wstrzymal_sie", []))
        named_brak = len(nv.get("brak_glosu", []))
        named_nieob = len(nv.get("nieobecni", []))

        # Always prefer named_votes counts — they come from actual name lists
        if named_za + named_przeciw + named_wstrz + named_brak + named_nieob > 0:
            c["za"] = named_za
            c["przeciw"] = named_przeciw
            c["wstrzymal_sie"] = named_wstrz
            c["brak_glosu"] = named_brak
            c["nieobecni"] = named_nieob

        # Recalculate resolution from corrected counts
        total_active = c["za"] + c["przeciw"] + c["wstrzymal_sie"]
        if total_active > 0:
            v["resolution"] = "przyjęta" if c["za"] > total_active / 2 else "odrzucona"

    return votes


def parse_votes_from_file(filepath: Path, att: dict) -> list[dict]:
    """Parse voting data from a PDF or DOCX file."""
    if att["format"] == "docx":
        votes = _parse_votes_docx(filepath, att)
    elif att["format"] == "pdf":
        votes = _parse_votes_pdf(filepath, att)
    else:
        print(f"    UWAGA: Nieznany format: {att['format']}")
        return []

    return _reconcile_counts(votes)


def _extract_text_docx(filepath: Path) -> str:
    """Extract plain text from a DOCX file.

    Tries pandoc first (better formatting), falls back to python-docx
    if pandoc is not installed.
    """
    try:
        result = subprocess.run(
            ["pandoc", str(filepath), "-t", "plain", "--wrap=none"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        pass  # pandoc not installed, fall through to python-docx
    except Exception as e:
        print(f"    UWAGA pandoc: {e}")

    # Fallback: python-docx (already a dependency)
    try:
        from docx import Document
        doc = Document(str(filepath))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        print(f"    BŁĄD odczytu docx {filepath.name}: {e}")
        return ""


def _extract_text_pdf(filepath: Path) -> str:
    """Extract text from a PDF file using PyMuPDF."""
    try:
        doc = fitz.open(str(filepath))
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        doc.close()
        return text
    except Exception as e:
        print(f"    BŁĄD PyMuPDF: {e}")
        return ""


def _parse_votes_docx(filepath: Path, att: dict) -> list[dict]:
    """Parse votes from a DOCX file (sessions XVII+).

    Format:
        Wyniki głosowania
        Głosowano w sprawie: [topic]
        ZA: N, PRZECIW: M, WSTRZYMUJĘ SIĘ: K, BRAK GŁOSU: L, NIEOBECNI: O
        Wyniki imienne:
        ZA (N)
        Name1, Name2, Name3
        PRZECIW (M)
        Name4
        WSTRZYMUJĘ SIĘ (K)
        BRAK GŁOSU (L)
        NIEOBECNI (O)
        Głosowanie z dnia: DD.MM.YYYY, HH:MM:SS
    """
    text = _extract_text_docx(filepath)
    if not text:
        return []
    return _parse_votes_text(text, att)


def _ocr_page(page) -> str:
    """OCR a single PDF page using Tesseract via pixmap rendering."""
    import subprocess
    import tempfile
    import os

    pix = page.get_pixmap(dpi=300)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
        pix.save(tmp_path)

    try:
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "-l", "pol", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout
    except FileNotFoundError:
        print("    UWAGA: tesseract nie jest zainstalowany, pomijam OCR")
        return ""
    except subprocess.TimeoutExpired:
        print("    UWAGA: timeout OCR")
        return ""
    finally:
        os.unlink(tmp_path)


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


def _parse_votes_pdf(filepath: Path, att: dict) -> list[dict]:
    """Parse votes from a PDF file (sessions I–XVI).

    PDF format is completely different from DOCX:
    - Each page is one vote ("RAPORT PRZEPROWADZONEGO GŁOSOWANIA")
    - Individual votes are listed as numbered rows: "1  Name  ZA"
    - OCR artifacts may corrupt names (e.g. "Meier" → "Meler")
    - Vote types: ZA, PRZECIW, WSTRZYMAŁ (SIĘ), NIE GŁOSOWAŁ, NIEOBECNY

    Falls back to DOCX-style parsing if "RAPORT PRZEPROWADZONEGO" is not found.
    Scanned PDFs (no embedded text) are OCR'd via Tesseract.
    """
    try:
        doc = fitz.open(str(filepath))
    except Exception as e:
        print(f"    BŁĄD PyMuPDF: {e}")
        return []

    # Check if this is the "RAPORT" format or DOCX-like format
    first_page_text = doc[0].get_text() if len(doc) > 0 else ""

    # If no embedded text, try OCR
    is_scanned = len(first_page_text.strip()) < 50
    if is_scanned:
        print(f"    Skan — uruchamiam OCR ({len(doc)} stron)...")
        first_page_text = _ocr_page(doc[0])

    # Check for RAPORT format (OCR may render GŁOSOWANIA as GLOSOWANIA)
    is_raport = bool(re.search(r'RAPORT\s+PRZEPROWADZONEGO\s+G[ŁL]?OSOWANIA', first_page_text))

    if not is_raport:
        # Not the scanned report format — try DOCX-style (eSesja) parsing
        if is_scanned:
            # OCR all pages and concatenate
            text = first_page_text + "\n"
            for page_idx in range(1, len(doc)):
                text += _ocr_page(doc[page_idx]) + "\n"
            doc.close()
        else:
            doc.close()
            text = _extract_text_pdf(filepath)
        if not text:
            return []
        return _parse_votes_text(text, att)

    # Parse each page as a separate vote (RAPORT format)
    votes = []
    for page_idx in range(len(doc)):
        if is_scanned:
            page_text = _ocr_page(doc[page_idx]) if page_idx > 0 else first_page_text
        else:
            page_text = doc[page_idx].get_text()
        vote = _parse_pdf_report_page(page_text, att, page_idx)
        if vote:
            votes.append(vote)

    doc.close()
    return votes


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(
                curr_row[j] + 1,       # insert
                prev_row[j + 1] + 1,   # delete
                prev_row[j] + cost,    # replace
            ))
        prev_row = curr_row

    return prev_row[-1]


def _fuzzy_match_councilor(name: str) -> str | None:
    """Try to match an OCR-corrupted name to a known councilor.

    Uses Levenshtein edit distance for robust matching against OCR errors
    like character substitution, insertion, deletion.
    Returns canonical name if match found, None otherwise.
    """
    if not KNOWN_COUNCILORS:
        return name

    # Normalize: lowercase, strip extra spaces/hyphens
    norm = _normalize_name(name)

    # Exact match first
    if norm in COUNCILOR_CANONICAL:
        return COUNCILOR_CANONICAL[norm]

    # Also try with normalized spaces around hyphens
    norm_dehyph = re.sub(r'\s*-\s*', '-', norm)
    if norm_dehyph in COUNCILOR_CANONICAL:
        return COUNCILOR_CANONICAL[norm_dehyph]

    # Fuzzy match using Levenshtein distance
    best_match = None
    best_distance = 999

    for known_norm, canonical in COUNCILOR_CANONICAL.items():
        longer = max(len(norm), len(known_norm))
        if longer == 0:
            continue
        if abs(len(norm) - len(known_norm)) > 6:
            continue  # Too different in length

        dist = _levenshtein_distance(norm, known_norm)
        if dist < best_distance:
            best_distance = dist
            best_match = canonical

    # Allow up to 25% edit distance (e.g. 5 edits for 20-char name)
    max_dist = max(len(norm), 1) * 0.25
    if best_distance <= max(max_dist, 3):
        return best_match

    return None


def _strip_diacritics(text: str) -> str:
    """Remove Polish diacritics for OCR comparison."""
    table = str.maketrans('ąćęłńóśżźĄĆĘŁŃÓŚŻŹ', 'acelnoszzACELNOSZZ')
    return text.translate(table)


def _fuzzy_match_councilor_generous(name: str) -> str | None:
    """Like _fuzzy_match_councilor but more generous for OCR-garbled handwritten text.

    Handwritten names in topics are much more garbled than printed names in
    individual vote rows, so we allow up to 45% edit distance.
    Also strips diacritics before comparing (OCR never produces Polish diacritics).
    Also tries partial matching (first name or last name only).
    """
    if not KNOWN_COUNCILORS:
        return None

    norm = _strip_diacritics(_normalize_name(name))
    if not norm or len(norm) < 3:
        return None

    # Exact match first (with diacritics stripped)
    for known_norm, canonical in COUNCILOR_CANONICAL.items():
        if _strip_diacritics(known_norm) == norm:
            return canonical

    # Fuzzy match using Levenshtein distance — generous threshold, no diacritics
    best_match = None
    best_distance = 999

    for known_norm, canonical in COUNCILOR_CANONICAL.items():
        known_stripped = _strip_diacritics(known_norm)
        if abs(len(norm) - len(known_stripped)) > 8:
            continue
        dist = _levenshtein_distance(norm, known_stripped)
        if dist < best_distance:
            best_distance = dist
            best_match = canonical

    # Allow up to 45% edit distance for handwritten OCR
    max_dist = max(len(norm), 1) * 0.45
    if best_distance <= max(max_dist, 4):
        return best_match

    # Try matching just the last word (surname) against councilor surnames
    words = norm.split()
    if len(words) >= 1:
        surname = words[-1]
        if len(surname) >= 4:
            best_match = None
            best_distance = 999
            for known_norm, canonical in COUNCILOR_CANONICAL.items():
                known_surname = _strip_diacritics(known_norm.split()[-1])
                dist = _levenshtein_distance(surname, known_surname)
                if dist < best_distance:
                    best_distance = dist
                    best_match = canonical
            # 50% tolerance on surname alone (handwritten OCR is very noisy)
            if best_distance <= max(len(surname) * 0.50, 3):
                return best_match

    return None


def _clean_ocr_topic(topic: str) -> str:
    """Clean up OCR-garbled vote topics.

    Fixes:
    - OCR artifacts (¢, |, stray punctuation)
    - "fan"/"far" → "Pan" (common OCR misread of handwritten "Pan")
    - Garbled handwritten candidate names → fuzzy match against known councilors
    - Trailing junk after candidate names
    """
    if not topic:
        return topic

    # Strip leaked OCR labels that weren't removed by the header parser (may appear multiple times)
    for _ in range(3):  # max 3 iterations
        new = re.sub(r'^(?:Temat|Data)\s+g\S*osowania\s*:\s*', '', topic, flags=re.IGNORECASE).strip()
        new = re.sub(r'^[—–\-_=\s.]+', '', new).strip()
        if new == topic:
            break
        topic = new

    # Remove OCR artifacts
    topic = topic.replace('¢', 'ć')
    # Replace pipe (|) only when it looks like OCR artifact (surrounded by letters), not in "Druk Nr"
    topic = re.sub(r'(?<=[a-ząęóśżźćńł])\|(?=[a-ząęóśżźćńł])', 'l', topic, flags=re.IGNORECASE)
    # Strip leading/trailing stray chars
    topic = re.sub(r'^[—–\-_=\s.]+', '', topic).strip()
    topic = re.sub(r'[|]+\s*$', '', topic).strip()

    # Fix "fan"/"far" → "Pan" (OCR misread of handwritten "Pan/Pani")
    topic = re.sub(r'\bfan\b', 'Pan', topic, flags=re.IGNORECASE)
    topic = re.sub(r'\bfar\b', 'Pan', topic, flags=re.IGNORECASE)
    topic = re.sub(r'\bPaw\b', 'Pan', topic)  # "Paw" → "Pan"

    # For "wybór/wybor" votes with candidate names, try to reconstruct
    # garbled handwritten names using fuzzy matching against councilors
    if KNOWN_COUNCILORS and re.search(r'wyb[oó]r\b', topic, re.IGNORECASE):
        topic = _fix_candidate_in_topic(topic)
    elif KNOWN_COUNCILORS and re.match(r'^(?:Pan[i]?\s+)?(\S+\s+\S+)', topic):
        # Topic looks like just a garbled name (OCR lost the actual topic text)
        # Try matching against councilors — if it matches, it's likely a candidate name
        # from a "wybór" vote where the printed part was lost
        name_match = re.match(r'^(?:Pan[i]?\s+)?(.+?)[:;,.\s]*$', topic)
        if name_match:
            candidate = name_match.group(1).strip()
            matched = _fuzzy_match_councilor_generous(candidate)
            if matched:
                topic = f"wybór — kandydat(ka): {matched}"

    # Clean up stray trailing punctuation/whitespace
    topic = re.sub(r'[\s,;:]+$', '', topic).strip()

    return topic


def _fix_candidate_in_topic(topic: str) -> str:
    """For 'wybór' vote topics, try to fix garbled candidate names.

    Handwritten candidate names (e.g. '— Pan Marcin Stefański') are often
    garbled by OCR (e.g. '~ far Martin Stefan nck,'). This function tries
    to identify and fix them using fuzzy matching against known councilors.
    """
    # Pattern: after a dash/tilde, there may be "Pan/Pani" and a garbled name
    # Try to find the candidate section (after —, ~, -, or =)
    sep_match = re.search(r'[—–\-~=]\s*(?:Pan[i]?\s+)?(.+)$', topic)
    if not sep_match:
        return topic

    candidate_text = sep_match.group(1).strip()
    # Remove trailing junk (random chars from OCR)
    candidate_text = re.sub(r'[,;:|~=\-—–\s]+$', '', candidate_text).strip()

    if not candidate_text or len(candidate_text) < 3:
        # Candidate text too short, can't match — remove garbled part
        topic = topic[:sep_match.start()].strip()
        topic = re.sub(r'[—–\-~=\s]+$', '', topic).strip()
        return topic

    # Try fuzzy matching (generous for handwritten OCR)
    matched = _fuzzy_match_councilor_generous(candidate_text)
    if matched:
        prefix = topic[:sep_match.start()].strip()
        prefix = re.sub(r'[—–\-~=\s]+$', '', prefix).strip()
        return f"{prefix} — kandydat(ka): {matched}"

    # If fuzzy match fails, try matching individual words as first/last name
    words = candidate_text.split()
    if len(words) >= 2:
        test_name = words[0] + ' ' + words[1]
        matched = _fuzzy_match_councilor_generous(test_name)
        if matched:
            prefix = topic[:sep_match.start()].strip()
            prefix = re.sub(r'[—–\-~=\s]+$', '', prefix).strip()
            return f"{prefix} — kandydat(ka): {matched}"

    # Can't fix candidate name — strip garbled trailing part
    topic = topic[:sep_match.start()].strip()
    topic = re.sub(r'[—–\-~=\s]+$', '', topic).strip()
    return topic


def _parse_pdf_report_page(text: str, att: dict, page_idx: int) -> dict | None:
    """Parse a single page from a PDF voting report.

    Format per page:
        RAPORT PRZEPROWADZONEGO GŁOSOWANIA
        Temat głosowania: [topic]
        ...
        Głosów ZA: N
        Głosów WSTRZ: M
        Głosów PRZECIW: K
        ...
        Głosy indywidualne:
        Lp.  Imię i Nazwisko  Głos
        1    Piotr Bagiński   ZA
        2    Barbara Brzezicka ZA
        ...
    """
    if not re.search(r'RAPORT\s+PRZEPROWADZONEGO', text):
        return None

    # --- Topic ---
    # OCR field ordering varies: labels may be grouped before values, or inline.
    # Strategy: extract block between "RAPORT..." and "Typ głosowania/gtosowania",
    # then strip known non-topic patterns (session name, date, labels).
    topic = ""

    header_block = re.search(
        r'RAPORT\s+PRZEPROWADZONEGO.*?\n(.*?)(?=Typ\s+g[łlt]osowania)',
        text, re.DOTALL | re.IGNORECASE
    )
    if header_block:
        raw = header_block.group(1)
        # Remove known labels (OCR: głosowania→gtosowania/giosowania/gfosowania, sesji→sesji)
        raw = re.sub(r'(?:Temat|Nazwa|Data)\s+(?:g\S*osowania|sesji)[;:]?\s*', '', raw, flags=re.IGNORECASE)
        # Remove session name pattern: "V sesja rady miasta 5-09-2024" or "| sesja Rady Miasta 7-05-2024"
        raw = re.sub(r'[IVXLCDM|]+\s+sesja\s+rady\s+miasta\s+\S+', '', raw, flags=re.IGNORECASE)
        # Remove dates: "05.09.2024" or "05 09 2024" or "05.09 2024" or "7-05-2024"
        raw = re.sub(r'\d{1,2}[.\s-]+\d{2}[.\s-]*\d{4}', '', raw)
        topic = re.sub(r'\s+', ' ', raw).strip()
        # Strip leading dashes/hyphens/underscores/equals
        topic = re.sub(r'^[—–\-_=\s.]+', '', topic).strip()
        # Clean OCR artifacts from topic
        topic = _clean_ocr_topic(topic)

    # --- Counts ---
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}

    # OCR-tolerant: Głosów→Gtosow/Glosow, WSTRZ can appear as WSTRZ
    za_m = re.search(r'G[łlt]os[óo6]w\s+ZA[;:]?\s*(\d+)', text, re.IGNORECASE)
    przeciw_m = re.search(r'G[łlt]os[óo6]w\s+PRZECIW[;:]?\s*(\d+)', text, re.IGNORECASE)
    wstrz_m = re.search(r'G[łlt]os[óo6]w\s+WSTRZ[;:]?\s*(\d+)', text, re.IGNORECASE)

    if za_m:
        counts["za"] = int(za_m.group(1))
    if przeciw_m:
        counts["przeciw"] = int(przeciw_m.group(1))
    if wstrz_m:
        counts["wstrzymal_sie"] = int(wstrz_m.group(1))

    # --- Resolution ---
    resolution = None
    text_lower = text.lower()
    # OCR: została→zostata/zostaia, podjęta→podjeta
    if re.search(r'zosta[łlt]a\s+podj[ęe]ta', text_lower):
        resolution = "przyjęta"
    if re.search(r'nie\s+zosta[łlt]a\s+podj[ęe]ta', text_lower):
        resolution = "odrzucona"

    # --- Individual votes ---
    named_votes = {
        "za": [], "przeciw": [], "wstrzymal_sie": [],
        "brak_glosu": [], "nieobecni": [],
    }

    # Find the individual votes section (OCR: indywidualne, Głos→Gtos)
    indywidualne_pos = text.find("indywidualne")
    if indywidualne_pos == -1:
        indywidualne_pos = text.find("indywiduain")  # OCR variant
    if indywidualne_pos == -1:
        m = re.search(r'G[łlt]os\s*\n', text)
        indywidualne_pos = m.start() if m else -1
    if indywidualne_pos == -1:
        return None

    votes_section = text[indywidualne_pos:]

    # Parse numbered rows: "1  Piotr Bagiński  ZA"
    # OCR can produce various artifacts: "ZA"→"Zk"/"Za", "WSTRZYMAŁ"→"WSTRZYMAE",
    # "GŁOSOWAŁ"→"GLOSOWAL", "NIEOBECNY"→"NIEOBECNY"
    # Line numbers may be garbled by OCR: "7"→"t", "1"→"l", etc.
    # Use [\dA-Za-z] for line numbers (OCR can turn digits into letters)
    vote_pattern = re.compile(
        r'^\s*[\dA-Za-z]{1,2}\s+(\S+[ \t]+\S[^\n]+?)[ \t]+'
        r'(Z[AaKk]|PRZECIW|WSTRZYMA[ŁŁLEF][ \t]*(?:SI[ĘEÉE])?|NI?E[ \t]+G[ŁL]?OSOWA[ŁL]|NIEOBECN[YA])\s*$',
        re.MULTILINE | re.IGNORECASE
    )

    for m in vote_pattern.finditer(votes_section):
        raw_name = m.group(1).strip()
        vote_type = m.group(2).strip().upper()

        # Clean up OCR artifacts from name
        raw_name = re.sub(r'\s+', ' ', raw_name)

        # Try fuzzy matching
        matched_name = _fuzzy_match_councilor(raw_name)
        if not matched_name:
            # If fuzzy match fails, use raw name but log it
            if KNOWN_COUNCILORS:
                print(f"      UWAGA: Nie rozpoznano '{raw_name}' (OCR?)")
            matched_name = raw_name

        # Categorize vote — OCR-tolerant
        vote_upper = vote_type.replace("K", "A")  # "ZK"→"ZA"
        if vote_upper in ("ZA", "ZA"):
            named_votes["za"].append(matched_name)
        elif "PRZECIW" in vote_upper:
            named_votes["przeciw"].append(matched_name)
        elif "WSTRZYMA" in vote_upper:
            named_votes["wstrzymal_sie"].append(matched_name)
        elif "OSOWA" in vote_upper:  # NIE GŁOSOWAŁ / NIE GLOSOWAŁ
            named_votes["brak_glosu"].append(matched_name)
        elif "NIEOBECN" in vote_upper:
            named_votes["nieobecni"].append(matched_name)

    total_named = sum(len(v) for v in named_votes.values())

    # Fallback: multi-line format (PyMuPDF extracts some PDFs with each field on its own line)
    # Format: "1\nName\nVOTE_TYPE\n2\nName\nVOTE_TYPE\n..."
    if total_named == 0:
        vote_type_re = re.compile(
            r'^(Z[AaKk]|PRZECIW|WSTRZYMA[ŁŁL]\w*|NI?E\s+G[ŁL]?OSOWA[ŁL]\w*|NIEOBECN[YA]\w*)$',
            re.IGNORECASE
        )
        lines = votes_section.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Look for pattern: number_line, name_line, vote_type_line
            if re.match(r'^\d{1,2}$', line):
                if i + 2 < len(lines):
                    name_line = lines[i + 1].strip()
                    type_line = lines[i + 2].strip()
                    if name_line and vote_type_re.match(type_line):
                        raw_name = re.sub(r'\s+', ' ', name_line)
                        matched_name = _fuzzy_match_councilor(raw_name)
                        if not matched_name:
                            if KNOWN_COUNCILORS:
                                print(f"      UWAGA: Nie rozpoznano '{raw_name}' (OCR?)")
                            matched_name = raw_name
                        vote_upper = type_line.strip().upper().replace("K", "A")
                        if vote_upper.startswith("ZA") or vote_upper == "ZA":
                            named_votes["za"].append(matched_name)
                        elif "PRZECIW" in vote_upper:
                            named_votes["przeciw"].append(matched_name)
                        elif "WSTRZYMA" in vote_upper:
                            named_votes["wstrzymal_sie"].append(matched_name)
                        elif "OSOWA" in vote_upper:
                            named_votes["brak_glosu"].append(matched_name)
                        elif "NIEOBECN" in vote_upper:
                            named_votes["nieobecni"].append(matched_name)
                        i += 3
                        continue
            i += 1
        total_named = sum(len(v) for v in named_votes.values())

    if total_named == 0:
        return None

    # Count nieobecni from named votes if not in counts
    if counts["nieobecni"] == 0:
        counts["nieobecni"] = len(named_votes["nieobecni"])
    if counts["brak_glosu"] == 0:
        counts["brak_glosu"] = len(named_votes["brak_glosu"])

    # --- Vote date ---
    vote_date = att.get("date")
    date_match = re.search(r'(\d{2})[.\s]+(\d{2})[.\s]+(\d{4})', text)
    if date_match:
        d, m, y = date_match.group(1), date_match.group(2), date_match.group(3)
        vote_date = f"{y}-{m}-{d}"

    # --- Druk number ---
    druk = None
    druk_match = re.search(r'Druk\s+Nr\s+(\d+)', text)
    if druk_match:
        druk = druk_match.group(1)

    session_number = att.get("number", "?")
    vote_id = f"{vote_date or 'unknown'}_{page_idx + 1:03d}"

    return {
        "id": vote_id,
        "source_url": att.get("url", ""),
        "session_date": vote_date,
        "session_number": session_number,
        "topic": topic[:500] if topic else f"Głosowanie {page_idx + 1}",
        "druk": druk,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


def _parse_votes_text(text: str, att: dict) -> list[dict]:
    """Parse voting data from extracted text (common for both PDF and DOCX).

    Splits on "Wyniki głosowania" markers, then parses each vote block.
    """
    # Split into individual vote blocks (case-insensitive, optional colon after header)
    blocks = re.split(r'(?=Wyniki [gG]łosowania[:\s]*\n)', text)
    votes = []

    for idx, block in enumerate(blocks):
        if not re.match(r'Wyniki [gG]łosowania', block.strip()):
            continue

        vote = _parse_single_vote(block, att, idx)
        if vote:
            votes.append(vote)

    return votes


def _parse_single_vote(block: str, att: dict, vote_idx: int) -> dict | None:
    """Parse a single vote block.

    Returns a vote dict in Radoskop format.
    """
    lines = block.strip().split('\n')

    # --- Topic ---
    topic = ""
    # Match topic up to the counts line (ZA followed by number) or "Wyniki imienne"
    topic_match = re.search(
        r'Głosowano\s+w\s+sprawie:\s*(.+?)(?=\n\s*ZA[\s.:\'",]+\d|\nWynik)',
        block, re.DOTALL
    )
    if topic_match:
        topic = re.sub(r'\s+', ' ', topic_match.group(1)).strip()

    if not topic:
        # Try simpler match — just the first line after "Głosowano w sprawie:"
        for line in lines:
            if line.strip().startswith("Głosowano w sprawie:"):
                topic = line.strip().replace("Głosowano w sprawie:", "").strip()
                break

    # --- Counts ---
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}

    # OCR-tolerant counts parsing: separators can be :.'," or space
    # e.g. "ZA 20, PRZECIW. 0, WSTRZYMUJĘ SIĘ' 1, BRAK GŁOSU: 0, NIEOBECNI 0"
    _SEP = r'[.:\'",]?\s*'  # flexible separator after category name

    za_m = re.search(r'\bZA' + _SEP + r'(\d+)', block)
    przeciw_m = re.search(r'\bPRZECIW' + _SEP + r'(\d+)', block, re.IGNORECASE)
    wstrz_m = re.search(r'WSTRZYMUJ[ĘE]\s+SI[ĘE]' + _SEP + r'(\d+)', block, re.IGNORECASE)
    brak_m = re.search(r'BRAK\s+G[ŁL]OSU' + _SEP + r'(\d+)', block, re.IGNORECASE)
    nieob_m = re.search(r'NIEOBECNI' + _SEP + r'(\d+)', block, re.IGNORECASE)

    if za_m:
        counts["za"] = int(za_m.group(1))
    if przeciw_m:
        counts["przeciw"] = int(przeciw_m.group(1))
    if wstrz_m:
        counts["wstrzymal_sie"] = int(wstrz_m.group(1))
    if brak_m:
        counts["brak_glosu"] = int(brak_m.group(1))
    if nieob_m:
        counts["nieobecni"] = int(nieob_m.group(1))

    # --- Named votes ---
    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    # Find the "Wyniki imienne" section — OCR can garble this
    # e.g. "Wyniki imienne:", "Wyniki imienne", "Wynikumienpe."
    imienne_match = re.search(r'Wyniki\s*imienn\w*[.:;]?\s*\n', block, re.IGNORECASE)
    if not imienne_match:
        # Try OCR-garbled variants: "Wynik" followed by something vaguely like "imienne"
        imienne_match = re.search(r'Wynik\w{0,3}\s*imienn\w*[.:;]?\s*\n', block, re.IGNORECASE)
    if not imienne_match:
        # Last resort: look for "ZA (\d+)" pattern that starts the names section
        imienne_match = re.search(r'\n(?=ZA\s*\(\d+\))', block)
    if not imienne_match:
        # No named votes
        if counts["za"] + counts["przeciw"] + counts["wstrzymal_sie"] > 0:
            print(f"    UWAGA: Brak wyników imiennych w głosowaniu {vote_idx + 1}")
        return None

    imienne_text = block[imienne_match.start():]

    # Parse named categories
    # Pattern: "ZA (17)\nName1, Name2, ...\nPRZECIW (0)\n..."
    categories = [
        (r'ZA\s*\(\d+\)', "za"),
        (r'PRZECIW\s*\(\d+\)', "przeciw"),
        (r'WSTRZYMUJ[ĘE]\s+SI[ĘE]\s*\(\d+\)', "wstrzymal_sie"),
        (r'BRAK\s+G[ŁL]OSU\s*\(\d+\)', "brak_glosu"),
        (r'NIEOBECNI\s*\(\d+\)', "nieobecni"),
    ]

    # Find positions of each category header
    cat_positions = []
    for pattern, cat_key in categories:
        m = re.search(pattern, imienne_text, re.IGNORECASE)
        if m:
            cat_positions.append((m.start(), m.end(), cat_key))

    # Also find "Głosowanie z dnia" as end marker (OCR can garble the colon)
    end_match = re.search(r'Głosowanie\s+z\s+dnia[.:;,]?', imienne_text, re.IGNORECASE)
    if not end_match:
        # Fallback: page footer patterns
        end_match = re.search(r'\d{2}[\s.]+\d{2}[\s.]+\d{4}\s*[|/]?\s*Wygenerowano', imienne_text)
    end_pos = end_match.start() if end_match else len(imienne_text)

    # Sort by position
    cat_positions.sort(key=lambda x: x[0])

    # Extract names for each category
    for i, (start, header_end, cat_key) in enumerate(cat_positions):
        # Text between this header and the next header (or end)
        if i + 1 < len(cat_positions):
            next_start = cat_positions[i + 1][0]
        else:
            next_start = end_pos

        names_text = imienne_text[header_end:next_start].strip()

        # Parse comma-separated names
        names = _parse_name_list(names_text)
        named_votes[cat_key] = names

    total_named = sum(len(v) for v in named_votes.values())
    if total_named == 0:
        return None

    # --- Druk number ---
    druk = None
    druk_match = re.search(r'Druk\s+Nr\s+(\d+)', block)
    if druk_match:
        druk = druk_match.group(1)

    # --- Resolution status ---
    resolution = None
    total_za = counts["za"]
    total_all = counts["za"] + counts["przeciw"] + counts["wstrzymal_sie"]
    if total_all > 0 and total_za > total_all / 2:
        resolution = "przyjęta"
    elif total_all > 0:
        resolution = "odrzucona"

    # --- Vote date ---
    vote_date = att.get("date")
    # OCR-tolerant date: "30.01.2025" or "30 01 2025" or "30 01.2025"
    date_match = re.search(
        r'Głosowanie\s+z\s+dnia[.:;]?\s*(\d{2})[.\s]+(\d{2})[.\s]+(\d{4})',
        block
    )
    if date_match:
        d, m, y = date_match.group(1), date_match.group(2), date_match.group(3)
        vote_date = f"{y}-{m}-{d}"

    session_number = att.get("number", "?")
    vote_id = f"{vote_date or 'unknown'}_{vote_idx + 1:03d}"

    return {
        "id": vote_id,
        "source_url": att.get("url", ""),
        "session_date": vote_date,
        "session_number": session_number,
        "topic": topic[:500] if topic else f"Głosowanie {vote_idx + 1}",
        "druk": druk,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


def _parse_name_list(text: str) -> list[str]:
    """Parse a comma-separated list of councilor names.

    Input: "Piotr Bagiński, Barbara Brzezicka, Adam Gil"
    Output: ["Piotr Bagiński", "Barbara Brzezicka", "Adam Gil"]
    """
    # Clean up the text
    text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return []

    # Split by comma
    raw_names = [n.strip() for n in text.split(',')]

    clean_names = []
    for name in raw_names:
        name = name.strip()
        if not name or len(name) < 3:
            continue

        # Skip non-name patterns (numbers, headers, etc.)
        if re.match(r'^\d', name):
            continue
        if name.upper() in ("ZA", "PRZECIW", "NIEOBECNI", "BRAK"):
            continue

        # Validate against known councilors (exact or fuzzy for OCR)
        if _is_known_councilor(name):
            canonical = COUNCILOR_CANONICAL.get(_normalize_name(name), name)
            clean_names.append(canonical)
        elif KNOWN_COUNCILORS:
            # Try fuzzy match for OCR-corrupted names
            fuzzy = _fuzzy_match_councilor(name)
            if fuzzy:
                clean_names.append(fuzzy)

    return clean_names


# ---------------------------------------------------------------------------
# Step 4: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping."""
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path}")
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

        for cat, key in [("za", "votes_za"), ("przeciw", "votes_przeciw"),
                         ("wstrzymal_sie", "votes_wstrzymal"), ("brak_glosu", "votes_brak"),
                         ("nieobecni", "votes_nieobecny")]:
            for name in v["named_votes"].get(cat, []):
                if name in councilors:
                    councilors[name][key] += 1
                    if cat not in ("nieobecni",):
                        councilors[name]["sessions_present"].add(v["session_date"])
                    if cat in ("za", "przeciw", "wstrzymal_sie"):
                        _check_rebellion(councilors[name], cat, club_majority, v)

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
            "a": a, "b": b,
            "club_a": name_to_club.get(a, "?"),
            "club_b": name_to_club.get(b, "?"),
            "score": score,
            "common_votes": len(common),
        })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    return pairs[:20], pairs[-20:][::-1]


def build_sessions(attachments: list[dict], all_votes: list[dict]) -> list[dict]:
    """Build session data from attachments and votes."""
    # Group votes by session number
    votes_by_session = defaultdict(list)
    for v in all_votes:
        votes_by_session[v["session_number"]].append(v)

    # Group votes by session date (for backward compat)
    votes_by_date = defaultdict(list)
    for v in all_votes:
        if v["session_date"]:
            votes_by_date[v["session_date"]].append(v)

    result = []
    seen_numbers = set()

    for att in attachments:
        number = att.get("number", "?")
        if number in seen_numbers:
            continue
        seen_numbers.add(number)

        # Get date from attachment or derive from votes
        date = att.get("date")
        session_votes = votes_by_session.get(number, [])

        if not date and session_votes:
            # Derive date from first vote in this session
            for sv in session_votes:
                if sv.get("session_date"):
                    date = sv["session_date"]
                    break

        if not date:
            continue

        if not session_votes:
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

    return sorted(result, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Profile merging (same as Gdynia)
# ---------------------------------------------------------------------------

def _make_slug(name: str) -> str:
    """Generate a URL-friendly slug from a Polish name."""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    slug = name.lower()
    for src, dst in replacements.items():
        slug = slug.replace(src, dst)
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def merge_profiles(profiles_path: str, councilors: list[dict], kid: str):
    """Merge voting stats from scraped councilors back into profiles.json."""
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path}")
        return

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    stats_by_name = {c["name"]: c for c in councilors}

    for profile in data.get("profiles", []):
        name = profile["name"]
        if "slug" not in profile:
            profile["slug"] = _make_slug(name)

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Sopotu (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista załączników, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--cache-dir", default=".cache", help="Katalog na pobrane pliki")
    parser.add_argument("--local-dir", default=None,
                        help="Katalog z lokalnymi plikami PDF/DOCX (pomija Playwright)")
    args = parser.parse_args()

    global DELAY, CACHE_DIR, KNOWN_COUNCILORS, COUNCILOR_CANONICAL
    DELAY = args.delay
    CACHE_DIR = args.cache_dir

    print("=== Radoskop Scraper: Rada Miasta Sopotu (BIP) ===")
    print(f"Źródło: {BIP_VOTES_PAGE}")
    print()

    # Load known councilors for name validation
    KNOWN_COUNCILORS, COUNCILOR_CANONICAL = load_known_councilors(args.profiles)
    if KNOWN_COUNCILORS:
        print(f"  Załadowano {len(KNOWN_COUNCILORS)} znanych radnych z {args.profiles}")
    else:
        print(f"  UWAGA: Brak profili — walidacja nazw wyłączona")

    # 1. Get attachment list
    if args.local_dir:
        print(f"\n[1/4] Używam lokalnych plików z {args.local_dir}")
        attachments = _build_attachments_from_local(args.local_dir)
    else:
        print(f"\n[1/4] Pobieranie listy załączników z BIP...")
        attachments = scrape_attachment_list_playwright()

    if not attachments:
        print("BŁĄD: Nie znaleziono załączników.")
        print(f"Sprawdź ręcznie: {BIP_VOTES_PAGE}")
        sys.exit(1)

    if args.max_sessions > 0:
        attachments = attachments[:args.max_sessions]
        print(f"  (ograniczono do {args.max_sessions} sesji)")

    if args.dry_run:
        print("\nZnalezione załączniki:")
        for a in attachments:
            print(f"  {a['number']:>8} | {a.get('date', '?'):>10} | {a['format']:>4} | {a['filename']}")
        return

    # 2. Download attachments
    if not args.local_dir:
        print(f"\n[2/4] Pobieranie plików ({len(attachments)} załączników)...")
        for att in attachments:
            filepath = download_attachment(att)
            if filepath:
                att["local_path"] = str(filepath)
    else:
        print(f"\n[2/4] Pliki lokalne — pomijam pobieranie")

    # 3. Parse votes
    print(f"\n[3/4] Parsowanie głosowań...")
    all_votes = []
    for att in attachments:
        local_path = att.get("local_path")
        if not local_path:
            continue

        filepath = Path(local_path)
        if not filepath.exists():
            continue

        print(f"\n  Sesja {att['number']} ({att.get('date', '?')}) — {att['format'].upper()}")
        votes = parse_votes_from_file(filepath, att)
        print(f"    Sparsowano {len(votes)} głosowań")
        all_votes.extend(votes)

    print(f"\n  Razem: {len(all_votes)} głosowań z {len(attachments)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        sys.exit(1)

    # 4. Build output
    print(f"\n[4/4] Budowanie pliku wyjściowego...")
    profiles = load_profiles(args.profiles)
    if profiles:
        print(f"  Załadowano profile: {len(profiles)} radnych")

    kid = "2024-2029"
    councilors = build_councilors(all_votes, attachments, profiles)
    sessions_data = build_sessions(attachments, all_votes)
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

    print(f"\nZapisano dane: {out_path}")
    named_v = sum(1 for v in all_votes if sum(len(nv) for nv in v["named_votes"].values()) > 0)
    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań ({named_v} z imiennymi), {len(councilors)} radnych")

    # Merge stats into profiles
    merge_profiles(args.profiles, councilors, kid)

    print("\nGotowe!")


def _build_attachments_from_local(local_dir: str) -> list[dict]:
    """Build attachment list from local directory of PDF/DOCX files."""
    local = Path(local_dir)
    if not local.exists():
        print(f"  BŁĄD: Katalog {local_dir} nie istnieje")
        return []

    attachments = []
    for f in sorted(local.iterdir()):
        if f.suffix.lower() in (".pdf", ".docx"):
            name = f.name
            fmt = "docx" if f.suffix.lower() == ".docx" else "pdf"
            info = _parse_attachment_filename(name)

            if info["session_num"] == 0:
                # Try to extract any Roman numeral
                m = re.search(r'([IVXLCDM]+)', name)
                if m:
                    info["number"] = m.group(1).upper()
                    info["session_num"] = roman_to_int(info["number"])

            attachments.append({
                "filename": name,
                "url": "",
                "format": fmt,
                "local_path": str(f),
                **info,
            })

    attachments.sort(key=lambda x: x.get("session_num", 0))
    print(f"  Znaleziono {len(attachments)} plików w {local_dir}")
    return attachments


if __name__ == "__main__":
    main()
