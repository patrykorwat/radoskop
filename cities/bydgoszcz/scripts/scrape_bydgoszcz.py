#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Bydgoszczy.

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny.

Źródło: bip.um.bydgoszcz.pl
BIP Bydgoszcz to standardowy HTML — nie wymaga JavaScript.
Używa requests + BeautifulSoup do scrapowania, PyMuPDF do PDF.

Struktura BIP:
  1. Lista imiennych wykazów głosowań: https://bip.um.bydgoszcz.pl/artykul/1211/5811/imienne-wykazy-glosowan-radnych-w-roku-2024-kadencja-2024-2029
  2. Każdy link to PDF z wynikami głosowania dla jednej sesji
  3. Format PDF: nagłówek z datą sesji + tabela "Lp. / Nazwisko i imię / Głos"
  4. Głosy: ZA, PRZECIW, WSTRZYMUJĘ SIĘ, NIEOBECNY/NIEOBECNA

Użycie:
    pip install requests beautifulsoup4 lxml pymupdf
    python scrape_bydgoszcz.py [--output docs/data.json] [--profiles docs/profiles.json]
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
from urllib.parse import urljoin, urlparse

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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BIP_BASE = "https://bip.um.bydgoszcz.pl/"
# Strona nadrzędna kategorii — linkuje do osobnych artykułów per ROK
# ("...imienne-wykazy-glosowan-radnych-w-roku-{YYYY}-kadencja-{KAD}"). BIP
# tworzy nowy artykuł co roku (2024→5811, 2025→7219, 2026→9839, ...), więc nie
# da się zaszyć jednego URL — odkrywamy roczniki dynamicznie.
VOTING_CATEGORY_URL = "https://bip.um.bydgoszcz.pl/artykuly/1211/imienne-wykazy-glosowan-radnych"
KADENCJA_SLUG = "2024-2029"
# Zachowane dla zgodności wstecznej / fallbacku, gdyby strona kategorii padła.
VOTING_LIST_URL = "https://bip.um.bydgoszcz.pl/artykul/1211/5811/imienne-wykazy-glosowan-radnych-w-roku-2024-kadencja-2024-2029"

KADENCJE = {
    "2024-2029": {
        "label": "IX kadencja (2024–2029)",
        "start": "2024-05-07",
    },
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://bydgoszcz.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DELAY = 0.5

# Mapper numerów sesji na daty — uzupełnić na podstawie dostępnych danych
# Format: "nr_sesji_roman" -> "2024-MM-DD"
SESSION_DATES = {
    # To be filled based on BIP data
}

# Skład Rady Miasta Bydgoszczy IX kadencji (2024-2029) — 28 mandatów.
# WAŻNE: kod to AKTUALNY KLUB RADNYCH wg oficjalnej listy BIP
# (bip.um.bydgoszcz.pl/artykul/1473/5809/kadencja-ix-2024-2029), NIE komitet
# wyborczy. Przypisanie musi pozostać aktualne — aktualizuj wg tej strony przy
# zmianach mandatów/klubów. Kluby: "KO i Lewica", "Bydgoska Prawica",
# "Niezrzeszeni" (radni z "Klub radnych: -" lub "Niezrzeszony" w BIP).
# Stan zweryfikowany 2026-05-30 (BIP akt. 20.01.2026 + PDF XXXI sesji):
#   - Anna Mackiewicz: mandat wygasł 31.07.2024 (została wiceprezydentem);
#     zachowana dla atrybucji głosów z 2024, w jej miejsce Aurelia Ratajczak.
#   - Katarzyna Zaczek (Bydgoska Prawica) — mandat w trakcie kadencji.
#   - Czerska-Thomas, Ginther, Hoppe, Dzakanowski są niezrzeszeni wg BIP.
COUNCILORS = {
    # Klub radnych Koalicja Obywatelska i Lewica (15)
    "Monika Matowska": "KO i Lewica",
    "Elżbieta Rusielewicz": "KO i Lewica",
    "Kazimierz Drozd": "KO i Lewica",
    "Lech Zagłoba-Zygler": "KO i Lewica",
    "Janusz Czwojda": "KO i Lewica",
    "Maria Gałęska": "KO i Lewica",
    "Marek Jeleniewski": "KO i Lewica",
    "Robert Kufel": "KO i Lewica",
    "Jakub Mikołajczak": "KO i Lewica",
    "Izabela Nowicka": "KO i Lewica",
    "Aurelia Ratajczak": "KO i Lewica",
    "Jan Szopiński": "KO i Lewica",
    "Maciej Świątkowski": "KO i Lewica",
    "Zdzisław Tylicki": "KO i Lewica",
    "Mateusz Zwolak": "KO i Lewica",
    # Była radna (mandat wygasł 31.07.2024) — dla atrybucji głosów z 2024
    "Anna Mackiewicz": "KO i Lewica",
    # Klub radnych Bydgoska Prawica (8)
    "Wojciech Bielawa": "Bydgoska Prawica",
    "Paweł Bokiej": "Bydgoska Prawica",
    "Jędrzej Gralik": "Bydgoska Prawica",
    "Michał Krzemkowski": "Bydgoska Prawica",
    "Szymon Róg": "Bydgoska Prawica",
    "Paweł Sieg": "Bydgoska Prawica",
    "Grażyna Szabelska": "Bydgoska Prawica",
    "Piotr Walczak": "Bydgoska Prawica",
    # Niezrzeszeni — brak klubu wg BIP (5)
    "Katarzyna Zaczek": "Niezrzeszeni",
    "Joanna Czerska-Thomas": "Niezrzeszeni",
    "Radosław Ginther": "Niezrzeszeni",
    "Tomasz Hoppe": "Niezrzeszeni",
    "Bogdan Dzakanowski": "Niezrzeszeni",
}

MONTHS_PL = {
    "stycznia": 1, "luty": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}

# Build a reverse lookup so "Lastname Firstname" also resolves to a club.
# PDFs use "Lastname Firstname" while COUNCILORS uses "Firstname Lastname".
def _build_name_lookup(councilors: dict[str, str]) -> dict[str, str]:
    lookup = {}
    for name, club in councilors.items():
        lookup[name] = club
        parts = name.split()
        if len(parts) == 2:
            lookup[f"{parts[1]} {parts[0]}"] = club
        elif len(parts) == 3:
            # Handle e.g. "Joanna Czerska-Thomas" -> "Czerska-Thomas Joanna"
            lookup[f"{parts[1]} {parts[2]} {parts[0]}"] = club
            lookup[f"{parts[2]} {parts[1]} {parts[0]}"] = club
    return lookup

COUNCILOR_LOOKUP = _build_name_lookup(COUNCILORS)


def _build_canonical_lookup(councilors: dict[str, str]) -> dict[str, str]:
    """Mapuje warianty zapisu (Nazwisko Imię, Imię Nazwisko) → kanoniczny zapis Imię Nazwisko."""
    lookup = {}
    for name in councilors:
        lookup[name] = name  # identity
        parts = name.split()
        if len(parts) == 2:
            lookup[f"{parts[1]} {parts[0]}"] = name
        elif len(parts) == 3:
            lookup[f"{parts[1]} {parts[2]} {parts[0]}"] = name
            lookup[f"{parts[2]} {parts[1]} {parts[0]}"] = name
    return lookup


CANONICAL_LOOKUP = _build_canonical_lookup(COUNCILORS)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# HTML disk cache - aktywowany przez --cache-dir. Pipeline scrape_all.sh
# przekazuje scratch_dir/.cache/html (persystentny między runami).
_CACHE_DIR: Path | None = None


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


def fetch(url: str, use_cache: bool = True) -> BeautifulSoup:
    """Pobiera stronę i zwraca BeautifulSoup.

    Disk cache aktywny gdy --cache-dir przekazany. Lista artykułów BIP
    zmienia się rzadko (nowe sesje co 2 tygodnie), więc cache jest bezpieczny.
    BIP Bydgoszcz nie zmienia historycznych URL-i.
    """
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        text = cache_file.read_text(encoding="utf-8")
        return BeautifulSoup(text, "html.parser")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if cache_file:
        try:
            cache_file.write_text(resp.text, encoding="utf-8")
        except Exception:
            pass
    return BeautifulSoup(resp.text, "html.parser")


def download_pdf(url: str, cache_dir: Path) -> Path | None:
    """Download a PDF to cache directory. Skip if already cached."""
    filename = url.split("/")[-1]
    if not filename.endswith(".pdf"):
        import hashlib
        filename = hashlib.md5(url.encode()).hexdigest() + ".pdf"

    path = cache_dir / filename

    if path.exists() and path.stat().st_size > 1000:
        print(f"    Cache hit: {filename}")
        return path

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        if b"%PDF" not in resp.content[:10]:
            print(f"    UWAGA: Nie PDF ({len(resp.content)} bytes)")
            return None
        path.write_bytes(resp.content)
        print(f"    Zapisano: {filename} ({len(resp.content)} bytes)")
        return path
    except Exception as e:
        print(f"    BŁĄD pobierania PDF {url}: {e}")
        return None


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


def fetch_pdf(url: str) -> bytes:
    """Pobiera PDF (legacy — bez cache)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

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


def roman_to_int(s: str) -> int | None:
    """Convert Roman numeral to int (I → 1, II → 2, etc.)."""
    s = s.upper().strip()
    val = 0
    i = 0
    rom_values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    while i < len(s):
        if i + 1 < len(s) and rom_values.get(s[i], 0) < rom_values.get(s[i+1], 0):
            val += rom_values.get(s[i+1], 0) - rom_values.get(s[i], 0)
            i += 2
        else:
            val += rom_values.get(s[i], 0)
            i += 1
    return val if val > 0 else None


# ---------------------------------------------------------------------------
# Step 1: Scrape voting list
# ---------------------------------------------------------------------------

def discover_year_pages() -> list[str]:
    """Ze strony kategorii zbiera URL-e artykułów rocznych dla danej kadencji.

    Każdy rok to osobny artykuł "...w-roku-{YYYY}-kadencja-{KAD}". Filtrujemy po
    slugu kadencji, żeby nie zaciągnąć starej kadencji 2018-2024.
    """
    try:
        soup = fetch(VOTING_CATEGORY_URL)
    except Exception as e:
        print(f"  BŁĄD: nie pobrałem strony kategorii: {e}")
        return []
    pages = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "imienne-wykazy-glosowan-radnych-w-roku" in href and KADENCJA_SLUG in href:
            pages.append(urljoin(BIP_BASE, href) if not href.startswith("http") else href)
    # Dedup zachowując kolejność (najnowszy rok pierwszy na stronie BIP).
    seen, out = set(), []
    for p in pages:
        if p not in seen:
            seen.add(p); out.append(p)
    return out


def _scrape_pdf_links_from_page(page_url: str) -> list[dict]:
    """Z jednej strony rocznej zbiera linki do PDF-ów z wynikami."""
    try:
        soup = fetch(page_url)
    except Exception as e:
        print(f"    BŁĄD strony {page_url}: {e}")
        return []
    pdf_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # Metryczki/podglądy mają #fragment lub nie są pobraniem — pomijamy.
        if ("attachments/download" in href or href.endswith(".pdf")) and "#" not in href:
            full_url = urljoin(BIP_BASE, href) if not href.startswith("http") else href
            pdf_links.append({"url": full_url, "title": text})
    return pdf_links


def scrape_voting_list() -> list[dict]:
    """Pobiera listę PDF-ów ze WSZYSTKICH stron rocznych kadencji."""
    year_pages = discover_year_pages()
    if not year_pages:
        print(f"  Brak stron rocznych z kategorii, fallback: {VOTING_LIST_URL}")
        year_pages = [VOTING_LIST_URL]
    else:
        print(f"  Strony roczne ({KADENCJA_SLUG}): {len(year_pages)}")

    pdf_links, seen = [], set()
    for page in year_pages:
        print(f"  Pobieram listę głosowań: {page}")
        for link in _scrape_pdf_links_from_page(page):
            if link["url"] in seen:
                continue
            seen.add(link["url"])
            pdf_links.append(link)

    print(f"  Znaleziono {len(pdf_links)} linków do PDF-ów (łącznie, wszystkie lata)")
    return pdf_links


# ---------------------------------------------------------------------------
# Step 2: Parse PDF voting data
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_data: bytes) -> str:
    """Ekstraktuje tekst z PDF-u."""
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        print(f"    Błąd przy parsowaniu PDF: {e}")
        return ""


def _classify_vote(line: str) -> str | None:
    """Classify a standalone vote line. Returns vote key or None."""
    t = line.strip().upper()
    if t == "ZA":
        return "za"
    if t == "PRZECIW":
        return "przeciw"
    if t in ("WSTRZYMUJĘ SIĘ", "WSTRZYMUJE SIE"):
        return "wstrzymal_sie"
    if t in ("NIEOBECNY", "NIEOBECNA"):
        return "nieobecny"
    if t == "NIEODDANY":
        return "nieoddany"
    return None


def _is_row_number(line: str) -> bool:
    """Check if line is a table row number like '1.' or '15.'."""
    return bool(re.match(r'^\d{1,2}\.$', line.strip()))


def _parse_single_page(page_text: str, url: str) -> dict | None:
    """Parse a single page of voting results.

    Each page in the Bydgoszcz BIP PDF represents one vote.
    The text structure (from PyMuPDF) is one element per line:
      - row number (e.g. "1.")
      - councillor name (e.g. "Bielawa Wojciech")
      - vote (e.g. "ZA")
    repeating in two-column order (left column items, then right column items,
    interleaved line by line).

    Returns dict with session_date, session_number, vote_title, votes, metadata
    or None if parsing fails.
    """
    result = {
        "session_date": None,
        "session_number": None,
        "vote_title": None,
        "votes": {},
        "metadata": {
            "url": url,
            "parsed_text": page_text[:500],
        }
    }

    # Extract date from "Data głosowania:  DD.MM.YYYY HH:MM"
    date_match = re.search(
        r'Data g\u0142osowania:\s*(\d{2})\.(\d{2})\.(\d{4})',
        page_text
    )
    if date_match:
        day, month, year = date_match.group(1), date_match.group(2), date_match.group(3)
        result["session_date"] = f"{year}-{month}-{day}"

    # Fallback: Polish text date "25 Listopada 2024 r."
    if not result["session_date"]:
        date_match2 = re.search(
            r'(\d{1,2})\s+(\w+)\s+(\d{4})\s*r\.?',
            page_text,
            re.IGNORECASE
        )
        if date_match2:
            day = date_match2.group(1)
            month_name = date_match2.group(2).lower()
            year = date_match2.group(3)
            month_num = MONTHS_PL.get(month_name, 0)
            if month_num:
                result["session_date"] = f"{year}-{month_num:02d}-{int(day):02d}"

    # Extract session number: "VIII Sesja Rady Miasta" (number before Sesja)
    session_match = re.search(
        r'([IVXLCDM]+)\s+[Ss]esja\b',
        page_text
    )
    if not session_match:
        # Fallback: "Sesja nr VI" or "Sesja VIII"
        session_match = re.search(
            r'[Ss]esja\s+(?:nr\.?\s+)?([IVXLCDM]+)',
            page_text
        )
    if session_match:
        result["session_number"] = session_match.group(1).upper()

    # Extract vote title (the agenda item being voted on)
    # It appears after the voting number line, e.g.:
    #   "1"  (vote sequence number)
    #   "3. Powołanie Komisji Uchwał i Wniosków."
    lines = page_text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^\d+\.\s+', stripped) and 'Sesja' not in stripped:
            # Likely an agenda item title. Długie tytuły w PDF-ach eSesja
            # zawijają się na kolejne wiersze; trzeba je skleić aż do pola
            # "Typ głosowania", inaczej tytuł urywa się w pół zdania.
            if i > 0 and lines[i - 1].strip().isdigit():
                title = stripped
                for cont in lines[i + 1:i + 12]:
                    c = cont.strip()
                    if not c or c.startswith('Typ głosowania'):
                        break
                    # Zawijanie z dywizem (np. "Kujawsko-\nPomorskiemu")
                    # łączymy bez spacji; pozostałe wiersze ze spacją.
                    title = title + c if title.endswith('-') else title + ' ' + c
                result["vote_title"] = re.sub(r'\s+', ' ', title).strip()
                break

    # Parse voting table: sequence of (row_number, name, vote) triplets
    # Lines appear as: "1.", "Bielawa Wojciech", "ZA", "15.", "Mikołajczak Jakub", "ZA", ...
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if _is_row_number(stripped):
            # Next line should be the name, line after that the vote
            if i + 2 < len(lines):
                name_line = lines[i + 1].strip()
                vote_line = lines[i + 2].strip()
                vote = _classify_vote(vote_line)
                if vote and name_line and len(name_line) > 2:
                    # PDF podaje "Nazwisko Imię" — normalizujemy do "Imię Nazwisko"
                    canonical = CANONICAL_LOOKUP.get(name_line, name_line)
                    club = COUNCILOR_LOOKUP.get(canonical, COUNCILOR_LOOKUP.get(name_line, ""))
                    result["votes"][canonical] = {
                        "vote": vote,
                        "club": club,
                    }
                    i += 3
                    continue
        i += 1

    return result if result["session_date"] else None


# Generyczne etykiety eSesja dla głosowań proceduralnych pod punktem porządku
# (wnioski/autopoprawki) nie mają opisu — PDF nazywa je "Głosowanie N". Jeśli w
# tej samej sesji ten punkt ma osobne głosowanie z realnym opisem (np.
# "5. Uchwalenie porządku obrad."), podpinamy ten opis, żeby tytuł nie był
# semantycznie pusty (było "5. Głosowanie 1".."5. Głosowanie 15").
_GENERIC_VOTE_LABEL_RE = re.compile(r'^Głosowanie\s+\d+\.?$')


def _split_agenda_title(title: str | None) -> tuple[str | None, str]:
    """'5. Uchwalenie porządku obrad.' → ('5', 'Uchwalenie porządku obrad.')."""
    m = re.match(r'^(\d+)\.\s*(.*)$', title or '')
    if m:
        return m.group(1), m.group(2).strip()
    return None, (title or '').strip()


def _enrich_generic_titles(records: list[dict]) -> None:
    """In-place: czyści tytuły głosowań w obrębie jednej sesji.

    1. Zdejmuje wiodący numer punktu porządku ("4. Uchwalenie..." → "Uchwalenie
       ..."). eSesja wkleja go w tytuł, a w listach SPA daje to podwójną
       numerację ("1. 4. Uchwalenie...") i wygląda jak literówka.
    2. Generyczne głosowania proceduralne ("Głosowanie K") podpina pod opisowy
       tytuł tego samego punktu, jeśli istnieje ("Uchwalenie porządku obrad
       (głosowanie K)"). Mapowanie po numerze punktu robione PRZED zdjęciem
       prefiksu. Punkty bez opisowego rodzeństwa (samotne "Głosowanie K")
       zostają jako goła etykieta. Separator w nawiasie, bez myślnika celowo.
    """
    point_desc: dict[str, str] = {}
    for r in records:
        pt, label = _split_agenda_title(r.get("vote_title"))
        if pt and label and not _GENERIC_VOTE_LABEL_RE.match(label):
            if pt not in point_desc or len(label) > len(point_desc[pt]):
                point_desc[pt] = label
    for r in records:
        pt, label = _split_agenda_title(r.get("vote_title"))
        if pt is None:
            continue  # tytuł bez numeru punktu — zostaw bez zmian
        if _GENERIC_VOTE_LABEL_RE.match(label):
            base = point_desc.get(pt)
            if base:
                n = re.search(r'(\d+)', label).group(1)
                r["vote_title"] = f"{base.rstrip(' .')} (głosowanie {n})"
            else:
                r["vote_title"] = label  # samotne "Głosowanie K"
        else:
            r["vote_title"] = label  # zdjęty prefiks numeru punktu


def parse_voting_pdf(pdf_data: bytes, url: str) -> list[dict]:
    """Parse a PDF with voting results. Returns a list of vote records (one per page).

    Each page in the Bydgoszcz BIP PDFs is a separate vote (agenda item).
    """
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        print(f"    Blad przy otwieraniu PDF: {e}")
        return []

    records = []
    for page in doc:
        text = page.get_text()
        if not text.strip():
            continue
        record = _parse_single_page(text, url)
        if record:
            records.append(record)

    # Wzbogacenie generycznych tytułów per sesja (jeden PDF = jedna sesja).
    _enrich_generic_titles(records)
    return records


# ---------------------------------------------------------------------------
# Step 3: Build data.json
# ---------------------------------------------------------------------------

def _compute_club_majority(vote_record: dict) -> dict:
    """For a single vote record, compute majority vote per club."""
    club_votes = defaultdict(lambda: defaultdict(int))
    for name, info in vote_record.get("votes", {}).items():
        club = COUNCILOR_LOOKUP.get(name, "")
        if club_has_line(club) and info["vote"] not in ("nieobecny",):
            club_votes[club][info["vote"]] += 1
    result = {}
    for club, cats in club_votes.items():
        if cats:
            result[club] = max(cats, key=cats.get)
    return result


def build_data_json(voting_records: list[dict]) -> dict:
    """Buduje strukturę data.json na podstawie zebranych danych głosowań."""

    # Convert each voting record to the named_votes format the template expects
    all_votes = []
    vote_id_counter = 0
    for record in voting_records:
        session_date = record.get("session_date")
        if not session_date:
            continue

        named_votes = {
            "za": [],
            "przeciw": [],
            "wstrzymal_sie": [],
            "brak_glosu": [],
            "nieobecni": [],
        }
        counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}

        for name, info in record.get("votes", {}).items():
            v = info["vote"]
            cat = {
                "za": "za",
                "przeciw": "przeciw",
                "wstrzymal_sie": "wstrzymal_sie",
                "nieobecny": "nieobecni",
            }.get(v, "brak_glosu")
            named_votes[cat].append(name)
            counts[cat] += 1

        vote_id_counter += 1
        all_votes.append({
            "id": str(vote_id_counter),
            "session_date": session_date,
            "session_number": record.get("session_number", ""),
            "topic": record.get("vote_title", "") or "",
            "named_votes": named_votes,
            "counts": {
                "za": counts["za"],
                "przeciw": counts["przeciw"],
                "wstrzymal_sie": counts["wstrzymal_sie"],
            },
        })

    # Build sessions
    sessions_by_date = {}
    for v in all_votes:
        d = v["session_date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {
                "date": d,
                "number": v.get("session_number", ""),
                "vote_count": 0,
                "attendees": set(),
                "speakers": [],
            }
        sessions_by_date[d]["vote_count"] += 1
        for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
            sessions_by_date[d]["attendees"].update(v["named_votes"].get(cat, []))

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": s["date"],
            "number": s["number"],
            "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]),
            "attendees": sorted(s["attendees"]),
            "speakers": s["speakers"],
        })

    # Build councilors with per-person vote stats
    all_names = set()
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    councilors_data = {}
    for name in sorted(all_names):
        club = COUNCILOR_LOOKUP.get(name, "Niezrzeszeni")
        councilors_data[name] = {
            "name": name,
            "club": club,
            "district": None,
            "votes_za": 0,
            "votes_przeciw": 0,
            "votes_wstrzymal": 0,
            "votes_brak": 0,
            "votes_nieobecny": 0,
            "votes_with_club": 0,
            "votes_against_club": 0,
            "rebellions": [],
        }

    for record in voting_records:
        if not record.get("session_date"):
            continue
        club_majority = _compute_club_majority(record)
        for name, info in record.get("votes", {}).items():
            if name not in councilors_data:
                continue
            c = councilors_data[name]
            v = info["vote"]
            if v == "za":
                c["votes_za"] += 1
            elif v == "przeciw":
                c["votes_przeciw"] += 1
            elif v == "wstrzymal_sie":
                c["votes_wstrzymal"] += 1
            elif v == "nieobecny":
                c["votes_nieobecny"] += 1
            else:
                c["votes_brak"] += 1

            # Rebellion check
            cat = {"za": "za", "przeciw": "przeciw", "wstrzymal_sie": "wstrzymal_sie"}.get(v)
            if cat:
                club = c["club"]
                if club_has_line(club) and club in club_majority:
                    if cat == club_majority[club]:
                        c["votes_with_club"] += 1
                    else:
                        c["votes_against_club"] += 1
                        c["rebellions"].append({
                            "vote_id": "",
                            "session": record.get("session_date", ""),
                            "topic": (record.get("vote_title") or "")[:120],
                            "their_vote": cat,
                            "club_majority": club_majority[club],
                        })

    # Frekwencja: ile sesji radny był obecny (zagłosował choć raz) / łączna liczba sesji.
    # Sesja = unikalna data. Radny jest "obecny" na sesji gdy ma chociaż jeden głos
    # inny niż "nieobecny" (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / brak głosu).
    total_sessions_count = len(sessions_data)
    councillor_sessions_present: dict[str, set] = defaultdict(set)
    for record in voting_records:
        d = record.get("session_date")
        if not d:
            continue
        for name, info in record.get("votes", {}).items():
            if info["vote"] != "nieobecny":
                councillor_sessions_present[name].add(d)

    total_votes_count = len(all_votes)
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes_count * 100) if total_votes_count > 0 else 0
        total_club = c["votes_with_club"] + c["votes_against_club"]
        zgodnosc = (c["votes_with_club"] / total_club * 100) if total_club > 0 else 0
        sessions_present = len(councillor_sessions_present.get(c["name"], set()))
        frekwencja = (sessions_present / total_sessions_count * 100) if total_sessions_count > 0 else 0.0
        councilors_list.append({
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
            "votes_total": total_votes_count,
            "rebellion_count": len(c["rebellions"]),
            "rebellions": c["rebellions"],
            "has_activity_data": False,
            "activity": None,
        })

    # Compute similarity pairs
    name_to_club = {c["name"]: c["club"] for c in councilors_list}
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat

    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
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
    sim_top = pairs[:20]
    sim_bottom = pairs[-20:][::-1]

    # Club counts
    club_counts = Counter()
    for c in councilors_list:
        club_counts[c["club"]] += 1

    kad = {
        "id": "2024-2029",
        "label": "IX kadencja (2024\u20132029)",
        "clubs": dict(sorted(club_counts.items())),
        "sessions": sessions_data,
        "total_sessions": len(sessions_data),
        "total_votes": total_votes_count,
        "total_councilors": len(councilors_list),
        "councilors": councilors_list,
        "votes": all_votes,
        "similarity_top": sim_top,
        "similarity_bottom": sim_bottom,
    }

    return {
        "generated": datetime.now().isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [kad],
    }


# ---------------------------------------------------------------------------
# Step 4: Build profiles.json
# ---------------------------------------------------------------------------

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


def build_profiles_json(voting_records: list[dict]) -> dict:
    """Buduje strukturę profiles.json na podstawie danych głosowań."""

    profiles_dict = {}

    councillor_votes = defaultdict(lambda: {
        "za": 0,
        "przeciw": 0,
        "wstrzymal_sie": 0,
        "nieobecny": 0,
        "nieoddany": 0,
        "brak": 0,
        "votes": [],
    })

    for record in voting_records:
        session_date = record.get("session_date")
        if not session_date:
            continue

        for name, vote_info in record.get("votes", {}).items():
            vote = vote_info["vote"]
            councillor_votes[name][vote] += 1
            councillor_votes[name]["votes"].append({
                "session": session_date,
                "vote": vote,
            })

    for councillor_name in sorted(councillor_votes.keys()):
        club = COUNCILOR_LOOKUP.get(councillor_name, "")
        votes_data = councillor_votes[councillor_name]

        votes_total = sum(votes_data[k] for k in ["za", "przeciw", "wstrzymal_sie", "nieobecny", "nieoddany"])

        if votes_total == 0:
            votes_total = 1

        # Frekwencja per sesja: unikalne daty gdzie radny był obecny (głos != nieobecny)
        sessions_present = len({
            v["session"] for v in votes_data["votes"] if v["vote"] != "nieobecny"
        })
        total_sessions = len({v["session"] for v in votes_data["votes"]})
        frekwencja = 100.0 * sessions_present / total_sessions if total_sessions > 0 else 0.0
        zgodnosc = 0.0

        profile = {
            "name": councillor_name,
            "slug": make_slug(councillor_name),
            "kadencje": {
                "2024-2029": {
                    "club": club if club else "Niezrzeszeni",
                    "has_voting_data": True,
                    "has_activity_data": False,
                    "frekwencja": round(frekwencja, 1),
                    "aktywnosc": 0.0,
                    "zgodnosc_z_klubem": round(zgodnosc, 1),
                    "votes_za": votes_data["za"],
                    "votes_przeciw": votes_data["przeciw"],
                    "votes_wstrzymal": votes_data["wstrzymal_sie"],
                    "votes_brak": votes_data["brak"],
                    "votes_nieobecny": votes_data["nieobecny"],
                    "votes_total": votes_total,
                    "rebellion_count": 0,
                    "rebellions": [],
                    "roles": [],
                    "notes": "",
                    "former": False,
                    "mid_term": False,
                }
            }
        }

        profiles_dict[councillor_name] = profile

    return {
        "profiles": list(profiles_dict.values())
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(output_data_path: str, output_profiles_path: str):
    """Główna funkcja scrapowania."""

    print("\n=== Scraper głosowań Rady Miasta Bydgoszczy ===\n")

    # Step 1: Get list of PDFs
    pdf_links = scrape_voting_list()
    if not pdf_links:
        print("  BŁĄD: Nie znaleziono żadnych PDF-ów!")
        return

    # Step 2: Download and parse each PDF
    cache_dir = Path("pdfs")
    cache_dir.mkdir(parents=True, exist_ok=True)

    voting_records = []
    for i, pdf_info in enumerate(pdf_links):
        url = pdf_info["url"]
        title = pdf_info["title"]

        print(f"  [{i+1}/{len(pdf_links)}] {title}")

        try:
            pdf_path = download_pdf(url, cache_dir)
            if not pdf_path:
                print(f"    ✗ Nie udalo sie pobrac PDF")
                continue
            pdf_data = pdf_path.read_bytes()
            records = parse_voting_pdf(pdf_data, url)
            if records:
                voting_records.extend(records)
                print(f"    ✓ Sesja: {records[0]['session_date']} (nr {records[0]['session_number']}), "
                      f"{len(records)} glosowan")
            else:
                print(f"    ✗ Nie udalo sie sparsowac")
        except Exception as e:
            print(f"    BŁĄD: {e}")

        time.sleep(DELAY)

    print(f"\n  Pobrano: {len(voting_records)} sesji\n")

    # Step 3: Build data.json
    print("  Buduję data.json...")
    data = build_data_json(voting_records)

    Path(output_data_path).parent.mkdir(parents=True, exist_ok=True)
    save_split_output(data, output_data_path)

    size_kb = Path(output_data_path).stat().st_size / 1024
    print(f"  Zapisano: {output_data_path} ({size_kb:.1f} KB)")

    # Step 4: Build profiles.json
    print("  Buduję profiles.json...")
    profiles = build_profiles_json(voting_records)

    Path(output_profiles_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_profiles_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

    size_kb = Path(output_profiles_path).stat().st_size / 1024
    print(f"  Zapisano: {output_profiles_path} ({size_kb:.1f} KB)")

    print("\n✓ Scrapowanie zakończone!")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper danych głosowań Rady Miasta Bydgoszczy"
    )
    parser.add_argument(
        "--output", default="docs/data.json",
        help="Ścieżka do pliku data.json (domyślnie: docs/data.json)"
    )
    parser.add_argument(
        "--profiles", default="docs/profiles.json",
        help="Ścieżka do pliku profiles.json (domyślnie: docs/profiles.json)"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML. Listy BIP i strony sesji cache'owane. '' albo brak = wyłączony."
    )
    args = parser.parse_args()

    init_cache(args.cache_dir)
    if args.cache_dir:
        print(f"Cache HTML: {args.cache_dir}")

    scrape(
        output_data_path=args.output,
        output_profiles_path=args.profiles,
    )


if __name__ == "__main__":
    main()
