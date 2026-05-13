#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Szczecin.

Źródło: https://bip.um.szczecin.pl/ (platforma BIP HTML)

Struktura BIP Szczecin:
  - Lista interpelacji/zapytań: https://bip.um.szczecin.pl/ (w sekcji odpowiedniej)
  - Szczegóły: artykuły z polami: typ, numer, radny, przedmiot, data

Kadencja IX (2024-2029) — przeszukiwanie sekcji interpelacji w BIP.

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja 2024-2029]
                                 [--debug]

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BIP_BASE = "https://bip.um.szczecin.pl/"

# Strona z wyszukiwarką interpelacji (ładuje dane przez AJAX/GET)
INTERPELACJE_PAGE = f"{BIP_BASE}chapter_50951.asp"
INTERPELACJE_AJAX = f"{BIP_BASE}contextsearch_xmldata_202FB59412A141BB91AFE00CE2C70636.asp"
ROWS_PER_PAGE = 30

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
    "2018-2024": {"label": "VIII kadencja (2018–2024)", "start": "2018-10-10"},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 1.0

# Polish month name → number
MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
    "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5,
    "czerwiec": 6, "lipiec": 7, "sierpień": 8, "wrzesień": 9,
    "październik": 10, "listopad": 11, "grudzień": 12, "styczeń": 1,
}


# ---------------------------------------------------------------------------
# HTTP helpers (z disk cache - patrz scripts/http_cache.py)
# ---------------------------------------------------------------------------

# Import shared cache helper z monorepo. sys.path manipulacja bo scrapery
# żyją w cities/*/scripts/ a util w scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_text, init_cache  # noqa: E402


def fetch_page(url: str, force: bool = False) -> BeautifulSoup | None:
    """Fetch and parse a page. Cache aktywny gdy --cache-dir przekazany."""
    try:
        text = cached_fetch_text(url, headers=HEADERS, delay=DELAY, force=force)
        return BeautifulSoup(text, "lxml")
    except Exception as e:
        print(f"  BŁĄD: {e}")
        return None


def parse_polish_date(text: str) -> str | None:
    """Parse '25 marca 2026 r.' → '2026-03-25'."""
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
# Parsing
# ---------------------------------------------------------------------------

def parse_title(title: str) -> tuple[str | None, int | None, int | None]:
    """
    Parse article title.
    Examples:
      "Interpelacja Nr 236/2026"
      "Zapytanie Nr 235/2026"
      "Interpelacja Nr 12/2024"
    Returns (typ, numer, rok) or (None, None, None).
    """
    m = re.match(
        r"(Interpelacja|Zapytanie|Intepelacja)\s+(?:Nr\s+)?(\d+)[./](\d{4})",
        title, re.IGNORECASE
    )
    if m:
        typ = m.group(1).lower().replace("intepelacja", "interpelacja")
        numer = int(m.group(2))
        rok = int(m.group(3))
        return typ, numer, rok
    return None, None, None


def extract_councilor_name(text: str) -> str:
    """
    Extract councilor name from text like:
      "Radnego: Jakub Świderski"
      "Radnej: Anna Kowalska"
    """
    m = re.search(r"Radn\w+:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def extract_subject(text: str) -> str:
    """Extract subject/topic from interpelacja text."""
    # Remove initial councilor line
    lines = text.split("\n")
    subject_lines = []
    skip_first = True
    for line in lines:
        if skip_first and ("Radn" in line or "dot." in line[:20]):
            skip_first = False
            continue
        if line.strip():
            subject_lines.append(line.strip())
    return " ".join(subject_lines[:3])  # First few lines


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_interpelacje_list(urls: list[str], debug: bool = False) -> list[dict]:
    """Scrape interpelacje from BIP Szczecin."""
    records = []

    for url in urls:
        soup = fetch_page(url)
        if not soup:
            continue

        # Find all article links
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]

            # Check if this looks like an interpelacja title
            if not re.search(r"(Interpelacja|Zapytanie|Intepelacja)\s+Nr", text, re.IGNORECASE):
                continue

            if not href.startswith("http"):
                href = requests.compat.urljoin(BIP_BASE, href)

            # Fetch the detail page
            detail_soup = fetch_page(href)
            if not detail_soup:
                continue

            # Parse details
            typ, numer, rok = parse_title(text)
            if not typ or not numer or not rok:
                if debug:
                    print(f"  [DEBUG] Nie sparsowano: {text}")
                continue

            # Extract content
            radny = ""
            przedmiot = ""
            data_wplywu = ""

            # Get article content
            content = detail_soup.find("div", class_=["content", "article", "main"])
            if not content:
                content = detail_soup.find("article") or detail_soup.body

            if content:
                full_text = content.get_text()
                radny = extract_councilor_name(full_text)
                przedmiot = extract_subject(full_text)

                # Try to find publication date
                # Format: "Data wpływu: 25 marca 2026 r."
                m = re.search(r"Data\s+wpływu[:\s]+(.+?)(?:\n|$)", full_text)
                if m:
                    data_wplywu = parse_polish_date(m.group(1)) or ""

            # CRI format: "NR/ROK" or "ZNR/ROK"
            cri = f"{numer}/{rok}"
            if typ == "zapytanie":
                cri = f"Z{cri}"

            records.append({
                "cri": cri,
                "typ": typ,
                "rok": rok,
                "kadencja": "2024-2029",  # Default to latest
                "radny": radny,
                "przedmiot": przedmiot[:200] if przedmiot else "",
                "data_wplywu": data_wplywu,
                "tresc_url": href,
                "odpowiedz_url": "",
                "data_odpowiedzi": "",
            })

    return records


def scrape_interpelacje_from_bip(kadencja: str = "2024-2029", debug: bool = False) -> list[dict]:
    """Pobierz interpelacje z AJAX endpoint BIP Szczecin.

    BIP Szczecin serwuje dane przez GET endpoint zwracający JSON
    z polem 'html' zawierającym tabelę HTML.
    Kolumny: Kadencja, Numer, Tytuł, Rodzaj, Data wpływu, Interpelujący, Data odpowiedzi.
    """
    import html as html_mod

    kadencja_roman = {"2024-2029": "IX", "2018-2024": "VIII"}.get(kadencja, "IX")

    print(f"\n=== Pobieranie interpelacji z BIP Szczecin ===")
    print(f"  Kadencja: {kadencja} ({kadencja_roman})")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = INTERPELACJE_PAGE

    # Inicjalizacja sesji (cookies)
    session.get(INTERPELACJE_PAGE, timeout=30)

    all_records = []
    page = 1

    while True:
        url = (
            f"{INTERPELACJE_AJAX}?mode=s&chapterid=&_search=false"
            f"&page={page}&rows={ROWS_PER_PAGE}&sidx=f7&sord=desc"
        )
        if debug:
            print(f"  GET page {page}: {url}")

        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  BLAD na stronie {page}: {e}")
            break

        total_pages = int(data.get("total", 0))
        total_records = int(data.get("records", 0))
        if page == 1:
            print(f"  Razem: {total_records} rekordow na {total_pages} stronach")

        html_content = html_mod.unescape(data.get("html", ""))
        if not html_content:
            break

        soup = BeautifulSoup(html_content, "lxml")
        table = soup.find("table")
        if not table:
            break

        rows = table.find_all("tr")[1:]  # skip header
        if not rows:
            break

        page_count = 0
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 7:
                continue

            kad = cells[0].get_text(strip=True)
            # Filtruj po kadencji
            if kad != kadencja_roman:
                continue

            numer = cells[1].get_text(strip=True)
            tytul = cells[2].get_text(strip=True)
            rodzaj = cells[3].get_text(strip=True).lower()
            data_wplywu = cells[4].get_text(strip=True)
            radny = cells[5].get_text(strip=True)
            data_odpowiedzi = cells[6].get_text(strip=True)

            # URL szczegółów
            link = row.find("a", href=True)
            detail_url = ""
            if link:
                href = link["href"]
                if not href.startswith("http"):
                    detail_url = BIP_BASE + href
                else:
                    detail_url = href

            typ = "interpelacja" if "interpelacja" in rodzaj else "zapytanie"

            cri = f"szczecin-{kadencja}-{numer}"

            record = {
                "cri": cri,
                "miasto": "szczecin",
                "kadencja": kadencja,
                "numer": numer,
                "typ": typ,
                "tytul": tytul,
                "radny": radny,
                "data_wplywu": data_wplywu,
                "data_odpowiedzi": data_odpowiedzi if data_odpowiedzi else None,
                "url": detail_url,
            }
            all_records.append(record)
            page_count += 1

        if debug:
            print(f"  Strona {page}: {page_count} rekordow (kadencja {kadencja_roman})")

        if page >= total_pages:
            break

        page += 1
        time.sleep(0.5)

    print(f"  Pobrano: {len(all_records)} rekordow")
    return all_records


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_records(records: list[dict], output_path: str):
    """Save interpelacje to JSON."""
    # Sort by date descending
    records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Deduplicate by CRI
    seen = set()
    unique = []
    for r in records:
        if r["cri"] not in seen:
            seen.add(r["cri"])
            unique.append(r)

    # Statistics
    interp = sum(1 for r in unique if r["typ"] == "interpelacja")
    zap = sum(1 for r in unique if r["typ"] == "zapytanie")

    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Razem:        {len(unique)}")

    # Save
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Szczecin"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego"
    )
    parser.add_argument(
        "--kadencja", default="2024-2029",
        help="Kadencja (2024-2029 lub 2018-2024)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML. Pipeline scrape_all.sh przekazuje scratch_dir/.cache/html."
    )
    args = parser.parse_args()

    init_cache(args.cache_dir)
    if args.cache_dir:
        print(f"Cache HTML: {args.cache_dir}")

    print("=== Radoskop Szczecin — Scraper Interpelacji ===")
    print(f"Backend: requests + BeautifulSoup\n")

    records = scrape_interpelacje_from_bip(kadencja=args.kadencja, debug=args.debug)

    if not records:
        print("\nUWAGA: Nie znaleziono żadnych interpelacji.")
        print("Może BIP Szczecin ma inną strukturę. Sprawdź ręcznie:")
        print(f"  {BIP_BASE}")
        # Utwórz pusty plik
        records = []

    save_records(records, args.output)


if __name__ == "__main__":
    main()
