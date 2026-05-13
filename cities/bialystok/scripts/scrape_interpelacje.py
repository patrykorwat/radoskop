#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Białystok.

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny

Źródło: https://www.bip.bialystok.pl/wladze/rada_miasta_bialystok/interpelacje-i-zapytania/

BIP Białystok wyświetla interpelacje jako listę rozwijalną z paginacją
opartą na formularzu POST (offset/limit). Każdy wpis zawiera:
  - Nr interpelacji
  - W sprawie (temat)
  - Data złożenia
  - Imię i nazwisko radnego

Strony szczegółowe zawierają załączniki PDF (skan interpelacji, odpowiedź).

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja IX]
                                 [--skip-details]
                                 [--debug]
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("Wymagany moduł: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Wymagany moduł: pip install beautifulsoup4")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.bip.bialystok.pl"

# Each kadencja has its own listing page on BIP.
# form_id is the HTML form element ID used for pagination requests.
KADENCJE = {
    "IX": {
        "label": "IX kadencja (2024–2029)",
        "path": "/wladze/rada_miasta_bialystok/interpelacje-i-zapytania/",
        "form_id": "PAGE_SEARCH_TYPE_INTERAPPELATIONS_2024_2029_FORM",
    },
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://bialystok.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,application/xhtml+xml",
}

DELAY = 0.5
PAGE_LIMIT = 10  # BIP shows 10 items per page


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_list_page(session, path, form_id, offset=0, limit=PAGE_LIMIT, debug=False):
    """Fetch one page of interpelacje listing via GET with pagination params.

    BIP Białystok uses a form with method=GET. The pagination requires
    three params: pagination[form] (the form ID), pagination[offset],
    and pagination[limit].
    """
    url = BASE_URL + path

    params = {
        "pagination[form]": form_id,
        "pagination[offset]": str(offset),
        "pagination[limit]": str(limit),
    }

    if debug:
        print(f"  [DEBUG] GET {url} offset={offset} limit={limit}")

    try:
        resp = session.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        if debug:
            print(f"  [DEBUG] Błąd pobierania: {e}")
        return None


def parse_list_page(html, kadencja_name, debug=False):
    """Parse interpelacje from a BIP listing page.

    Each entry is an h3 with a link, followed by text fields:
      Nr interpelacji: [Kadencja ...] NNN
      W sprawie: ...
      Data złożenia: YYYY-MM-DD
      Imię i nazwisko: [Kadencja ...] Firstname Lastname
    """
    if not html:
        return [], 0

    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Find all h3 elements that contain links to individual interpelacje
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "")
        if "interpelacj" not in href.lower() and "nr-interpelacji" not in href.lower():
            continue

        # Extract number from link text: "Nr interpelacji: [Kadencja 2024-2029] 316"
        link_text = a.get_text(strip=True)
        nr_match = re.search(r'(\d+)\s*$', link_text)
        nr = nr_match.group(1) if nr_match else ""

        full_url = href
        if href.startswith("/"):
            full_url = BASE_URL + href

        # The fields follow the h3 in the DOM. Walk siblings or look at the
        # parent container to find the text fields.
        parent = h3.parent
        if not parent:
            continue

        parent_text = parent.get_text(separator="\n")
        lines = [l.strip() for l in parent_text.split("\n") if l.strip()]

        przedmiot = ""
        data_zlozenia = ""
        radny = ""

        for line in lines:
            if line.lower().startswith("w sprawie:"):
                przedmiot = line.split(":", 1)[1].strip()
            elif line.lower().startswith("data złożenia:") or line.lower().startswith("data zlozenia:"):
                raw_date = line.split(":", 1)[1].strip()
                data_zlozenia = parse_date(raw_date)
            elif line.lower().startswith("imię i nazwisko:") or line.lower().startswith("imie i nazwisko:"):
                raw_name = line.split(":", 1)[1].strip()
                # Remove kadencja prefix: "[Kadencja 2024-2029] Name"
                raw_name = re.sub(r'\[.*?\]\s*', '', raw_name).strip()
                radny = raw_name

        record = {
            "nr": nr,
            "przedmiot": przedmiot,
            "radny": radny,
            "data_wplywu": data_zlozenia,
            "bip_url": full_url,
            "typ": "interpelacja",
            "kadencja": kadencja_name,
        }

        if record["przedmiot"] or record["nr"]:
            records.append(record)

    # Detect total items from pagination links (data-offset attributes)
    max_offset = 0
    for a_tag in soup.find_all("a", class_="page-search-filter-pagination-link"):
        offset_val = a_tag.get("data-offset", "0")
        try:
            off = int(offset_val)
            if off > max_offset:
                max_offset = off
        except ValueError:
            pass

    # Also check pagination text like "1 2 3 ... 32"
    for a_tag in soup.find_all("a"):
        text = a_tag.get_text(strip=True)
        if re.match(r'^\d+$', text):
            try:
                page_num = int(text)
                estimated_offset = (page_num - 1) * PAGE_LIMIT
                if estimated_offset > max_offset:
                    max_offset = estimated_offset
            except ValueError:
                pass

    total_pages = (max_offset // PAGE_LIMIT) + 1 if max_offset > 0 else 1

    if debug:
        print(f"  [DEBUG] Parsed {len(records)} records, max_offset={max_offset}, total_pages={total_pages}")

    return records, total_pages


# Import shared HTTP cache (sys.path manipulacja bo scraper jest pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_text, init_cache  # noqa: E402


def fetch_detail(session, bip_url, debug=False):
    """Fetch detail page for an interpelacja. Extract attachments and dates."""
    if not bip_url:
        return {}

    if debug:
        print(f"    [DEBUG] GET {bip_url}")

    try:
        text = cached_fetch_text(bip_url, session=session, headers=HEADERS, delay=0.1)
        soup = BeautifulSoup(text, "html.parser")
    except Exception as e:
        if debug:
            print(f"    [DEBUG] Błąd: {e}")
        return {}

    detail = {}
    page_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in page_text.split("\n") if l.strip()]

    for line in lines:
        lower = line.lower()
        if "data przekazania prezydentowi" in lower or "data przekazania" in lower:
            raw = line.split(":", 1)[-1].strip()
            detail["data_przekazania"] = parse_date(raw)
        elif "data publikacji" in lower:
            raw = line.split(":", 1)[-1].strip()
            detail["data_publikacji"] = parse_date(raw)

    # Find PDF attachments
    attachments = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        text = a_tag.get_text(strip=True)
        if ".pdf" in href.lower() or "resource" in href.lower():
            full_url = href
            if href.startswith("/"):
                full_url = BASE_URL + href
            attachments.append({"nazwa": text, "url": full_url})

            text_lower = text.lower()
            if "odpowied" in text_lower:
                detail["odpowiedz_url"] = full_url
                detail["odpowiedz_status"] = "udzielono odpowiedzi"
            elif not detail.get("tresc_url"):
                detail["tresc_url"] = full_url

    if attachments:
        detail["zalaczniki"] = attachments

    return detail


def parse_date(raw):
    """Convert date to YYYY-MM-DD format."""
    if not raw:
        return ""
    raw = raw.strip()
    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    # DD-MM-YYYY or DD.MM.YYYY
    m = re.match(r"(\d{2})[.\-](\d{2})[.\-](\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

CATEGORIES = {
    "transport": ["transport", "komunikacj", "autobus", "tramwaj", "drog", "ulic", "rondo",
                  "chodnik", "przejści", "parkow", "rower", "ścieżk"],
    "infrastruktura": ["infrastru", "remont", "naprawa", "budow", "inwesty", "moderniz",
                       "oświetl", "kanalizacj", "wodociąg"],
    "bezpieczeństwo": ["bezpiecz", "straż", "policj", "monitoring", "kradzież", "wandal"],
    "edukacja": ["szkoł", "edukacj", "przedszkol", "żłob", "nauczyc"],
    "zdrowie": ["zdrow", "szpital", "leczni", "medyc", "lekarz"],
    "środowisko": ["środowisk", "zieleń", "drzew", "park", "recykl", "odpady"],
    "mieszkalnictwo": ["mieszka", "lokal", "czynsz", "wspólnot"],
    "kultura": ["kultur", "bibliotek", "muzeum", "teatr", "koncert"],
    "sport": ["sport", "boisko", "stadion", "basen"],
    "pomoc społeczna": ["społeczn", "pomoc", "senior", "niepełnospr"],
    "budżet": ["budżet", "finansow", "wydatk", "podatk"],
    "administracja": ["administrac", "urzęd", "procedur"],
}


def classify_category(przedmiot):
    """Classify interpelacja category by keywords."""
    if not przedmiot:
        return "inne"
    text = przedmiot.lower()
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text:
                return cat
    return "inne"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(kadencje, output_path, fetch_details=True, debug=False):
    """Main scraping function."""
    session = requests.Session()
    all_records = []

    for kad_name in kadencje:
        kad = KADENCJE.get(kad_name)
        if not kad:
            print(f"Nieznana kadencja: {kad_name}")
            continue

        print(f"\n=== {kad['label']} ===")

        offset = 0
        total_pages = None
        kad_records = []
        page_num = 1

        while True:
            try:
                html = fetch_list_page(session, kad["path"], kad["form_id"],
                                       offset=offset, limit=PAGE_LIMIT,
                                       debug=debug)
                records, pages = parse_list_page(html, kad_name, debug=debug)
            except Exception as e:
                print(f"  BŁĄD na offset {offset}: {e}")
                break

            if total_pages is None:
                total_pages = max(pages, 1)
                print(f"  Łącznie stron: {total_pages}")

            kad_records.extend(records)

            if debug:
                print(f"  Strona {page_num}/{total_pages}: {len(records)} rekordów")
            elif page_num % 10 == 0:
                print(f"  Strona {page_num}/{total_pages}...")

            if not records or page_num >= total_pages:
                break

            offset += PAGE_LIMIT
            page_num += 1
            time.sleep(DELAY)

        print(f"  Pobrano: {len(kad_records)} rekordów")

        # Deduplicate by nr
        seen = set()
        unique = []
        for r in kad_records:
            key = r.get("nr", "") or r.get("bip_url", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(r)
        kad_records = unique
        if len(kad_records) != len(seen):
            print(f"  Po deduplikacji: {len(kad_records)} rekordów")

        # Fetch detail pages
        if fetch_details and kad_records:
            print(f"\n  Pobieram szczegóły ({len(kad_records)} rekordów)...")
            for i, rec in enumerate(kad_records):
                bip_url = rec.get("bip_url", "")
                if not bip_url:
                    continue
                detail = fetch_detail(session, bip_url, debug=debug)
                if detail:
                    rec.update({k: v for k, v in detail.items() if v})
                if (i + 1) % 50 == 0:
                    print(f"    Szczegóły: {i+1}/{len(kad_records)}")
                time.sleep(DELAY)

        all_records.extend(kad_records)

    # Classify and normalize
    for rec in all_records:
        rec["kategoria"] = classify_category(rec.get("przedmiot", ""))
        rec.setdefault("data_wplywu", "")
        rec.setdefault("data_odpowiedzi", rec.get("data_przekazania", ""))
        rec.setdefault("tresc_url", "")
        rec.setdefault("odpowiedz_url", "")
        rec.setdefault("odpowiedz_status", "")
        rec.setdefault("nr_sprawy", rec.get("nr", ""))

    # Sort newest first
    all_records.sort(
        key=lambda x: x.get("data_wplywu", "") or x.get("nr", ""),
        reverse=True,
    )

    # Stats
    interp = sum(1 for r in all_records if r.get("typ") == "interpelacja")
    zap = sum(1 for r in all_records if r.get("typ") == "zapytanie")
    answered = sum(1 for r in all_records if r.get("odpowiedz_url"))
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:        {len(all_records)}")

    # Save
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")
    print(f"Gotowe: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Białystok"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--kadencja", default="IX",
        help="Kadencja: IX lub 'all' (domyślnie: IX)"
    )
    parser.add_argument(
        "--skip-details", action="store_true",
        help="Pomiń pobieranie szczegółów"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML dla detail pages (pipeline przekazuje scratch/.cache/interp)."
    )
    args = parser.parse_args()

    init_cache(args.cache_dir)
    if args.cache_dir:
        print(f"Cache HTML interpelacji: {args.cache_dir}")

    if args.kadencja.lower() == "all":
        kadencje = list(KADENCJE.keys())
    else:
        kadencje = [k.strip() for k in args.kadencja.split(",")]

    scrape(
        kadencje=kadencje,
        output_path=args.output,
        fetch_details=not args.skip_details,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
