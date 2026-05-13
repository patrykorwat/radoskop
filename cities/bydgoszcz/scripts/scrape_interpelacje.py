#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Bydgoszcz.

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny.

Źródło: https://bip.um.bydgoszcz.pl/

BIP Bydgoszcz zawiera interpelacje w sekcji artykułów.
Każda interpelacja to osobna strona ze szczegółami, załącznikami i datami.

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja IX]
                                 [--fetch-details]
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

BASE_URL = "https://bip.um.bydgoszcz.pl"

# Interpelacje listing: /interpelacje/{page}/{perPage}
# The first page is at /interpelacje/1475 (BIP article ID for IX kadencja).
# Pagination: /interpelacje/{page}/{perPage}
INTERPELACJE_LIST_URL = f"{BASE_URL}/interpelacje"

KADENCJE = {
    "IX":   {"list_id": 1475, "label": "IX kadencja (2024-2029)"},
    "VIII": {"list_id": None, "label": "VIII kadencja (2018-2024)"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://bydgoszcz.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html",
}

DELAY = 0.5
PER_PAGE = 25

MONTHS_PL = {
    "stycznia": 1, "luty": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}


# ---------------------------------------------------------------------------
# Scraping: list page at /interpelacje/{page}/{perPage}
# ---------------------------------------------------------------------------

def fetch_list_page(http_session, page, per_page=PER_PAGE, debug=False):
    """Fetch a page of the interpelacje listing."""
    url = f"{INTERPELACJE_LIST_URL}/{page}/{per_page}"
    if debug:
        print(f"  [DEBUG] GET {url}")
    resp = http_session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_list_page(html, kadencja_name, debug=False):
    """Parse the interpelacje listing page.

    BIP Bydgoszcz lists interpelacje as table rows with th+td pairs.
    Each interpelacja entry has:
      - Title with link to /interpelacja/{id}/{slug}
      - Nr sprawy (case number)
      - Councillor name
    Pagination links use /interpelacje/{page}/{perPage} format.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    tables = main.find_all("table")

    records = []
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        record = {}
        for row in rows:
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue

            label = th.get_text(strip=True).lower()
            val_text = td.get_text(strip=True)

            if "w sprawie" in label:
                a = td.find("a")
                if a:
                    record["przedmiot"] = a.get_text(strip=True)
                    href = a.get("href", "")
                    if href.startswith("/"):
                        record["bip_url"] = BASE_URL + href
                    elif href.startswith("http"):
                        record["bip_url"] = href
                    m = re.search(r"/interpelacja/(\d+)/", href)
                    if m:
                        record["article_id"] = int(m.group(1))
                else:
                    record["przedmiot"] = val_text

                if "zapytanie" in label:
                    record["typ"] = "zapytanie"
                elif "wniosek" in label:
                    record["typ"] = "wniosek"
                else:
                    record["typ"] = "interpelacja"

            elif any(k in label for k in ["tożsamość", "radnego", "radnej", "autor"]):
                record["radny"] = val_text

            elif "nr sprawy" in label or "numer" in label:
                record["nr_sprawy"] = val_text

            elif "status" in label:
                record["status"] = val_text

        if record.get("przedmiot"):
            record.setdefault("radny", "")
            record.setdefault("status", "")
            record.setdefault("bip_url", "")
            record.setdefault("article_id", 0)
            record.setdefault("typ", "interpelacja")
            record.setdefault("nr_sprawy", "")
            record["kadencja"] = kadencja_name
            records.append(record)

    # If no table-based records found, try link-based parsing as fallback.
    # Some BIP pages list interpelacje as plain links with metadata text.
    if not records:
        for a in main.find_all("a", href=True):
            href = a.get("href", "")
            if "/interpelacja/" not in href:
                continue
            text = a.get_text(strip=True)
            if not text or len(text) < 5:
                continue

            record = {"przedmiot": text, "typ": "interpelacja", "kadencja": kadencja_name}
            if href.startswith("/"):
                record["bip_url"] = BASE_URL + href
            else:
                record["bip_url"] = href
            m = re.search(r"/interpelacja/(\d+)/", href)
            if m:
                record["article_id"] = int(m.group(1))

            # Look for sibling text with case number and councillor name
            parent = a.parent
            if parent:
                siblings_text = parent.get_text(separator="|")
                # Try to find "Nr sprawy: RM.0003..." pattern
                nr_match = re.search(r'(?:Nr sprawy|RM)\S*[\s:]*([A-Z]{2}\.\d[\d.]+\d{4})', siblings_text)
                if nr_match:
                    record["nr_sprawy"] = nr_match.group(1)

            record.setdefault("radny", "")
            record.setdefault("status", "")
            record.setdefault("bip_url", "")
            record.setdefault("article_id", 0)
            record.setdefault("nr_sprawy", "")
            records.append(record)

    # Extract total pages from pagination links: /interpelacje/{page}/{perPage}
    total_pages = 1
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r'/interpelacje/(\d+)/\d+', href)
        if m:
            p = int(m.group(1))
            if p > total_pages:
                total_pages = p
    # Also check plain page number links
    for a in soup.find_all("a"):
        txt = a.get_text(strip=True)
        if re.match(r"^\d+$", txt):
            p = int(txt)
            if p > total_pages:
                total_pages = p

    if debug:
        print(f"  [DEBUG] Parsed {len(records)} records, total_pages={total_pages}")

    return records, total_pages


# ---------------------------------------------------------------------------
# Scraping — detail page
# ---------------------------------------------------------------------------

# Import shared HTTP cache (sys.path bo scraper pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_text, init_cache  # noqa: E402


def fetch_detail(session, bip_url, debug=False):
    """Pobiera szczegóły interpelacji z jej strony."""
    if not bip_url:
        return {}

    if debug:
        print(f"  [DEBUG] GET {bip_url}")

    try:
        text = cached_fetch_text(bip_url, session=session, headers=HEADERS, delay=0.1)
        soup = BeautifulSoup(text, "html.parser")

        detail = {}
        # Parse table rows (th + td pairs)
        for row in soup.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue

            label = th.get_text(strip=True).lower()
            val = td.get_text(strip=True)

            if "typ wyst" in label:
                detail["typ_full"] = val
            elif "nr sprawy" in label or "numer" in label:
                detail["nr_sprawy"] = val
            elif "data wytworzenia" in label and "data_wplywu" not in detail:
                detail["data_wplywu"] = parse_date(val)

        # Find attachment links
        attachments = []
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if "attachments/download" in href or "zalacznik" in href:
                full_url = BASE_URL + href if href.startswith("/") else href
                attachments.append({"nazwa": text, "url": full_url})

                text_lower = text.lower()
                if "odpowied" in text_lower:
                    detail["odpowiedz_url"] = full_url
                elif not detail.get("tresc_url"):
                    detail["tresc_url"] = full_url

        if attachments:
            detail["zalaczniki"] = attachments

        return detail
    except Exception as e:
        if debug:
            print(f"  [DEBUG] Error fetching detail {bip_url}: {e}")
        return {}


def parse_date(raw):
    """Konwertuje datę na format YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    # DD.MM.YYYY or DD.MM.YYYY HH:MM
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    return raw


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

CATEGORIES = {
    "transport": ["transport", "komunikacj", "autobus", "tramwaj", "drog", "ulic", "rondo",
                  "chodnik", "przejści", "parkow", "rower", "ścieżk", "mpk", "przystank",
                  "sygnaliz", "skrzyżow"],
    "infrastruktura": ["infrastru", "remont", "naprawa", "budow", "inwesty", "moderniz",
                       "oświetl", "kanalizacj", "wodociąg", "nawierzch", "most"],
    "bezpieczeństwo": ["bezpiecz", "straż", "policj", "monitoring", "kradzież", "wandal",
                       "przestęp", "patrol"],
    "edukacja": ["szkoł", "edukacj", "przedszkol", "żłob", "nauczyc", "kształc",
                 "oświat", "uczni"],
    "zdrowie": ["zdrow", "szpital", "leczni", "medyc", "lekarz", "przychodni",
                "ambulat"],
    "środowisko": ["środowisk", "zieleń", "drzew", "park ", "recykl", "odpady",
                   "śmieci", "klimat", "ekolog", "powietrz", "smog", "hałas"],
    "mieszkalnictwo": ["mieszka", "lokal", "zasob", "czynsz", "wspólnot", "kamieni",
                       "dewelop", "budynek"],
    "kultura": ["kultur", "bibliotek", "muzeum", "teatr", "koncert", "festiwal",
                "zabytek", "zabytk"],
    "sport": ["sport", "boisko", "stadion", "basen", "siłowni", "hala sport",
              "rekrea"],
    "pomoc społeczna": ["społeczn", "pomoc", "bezdomn", "senior", "niepełnospr",
                        "opiek", "zasiłk"],
    "budżet": ["budżet", "finansow", "wydatk", "dotacj", "środki", "pieniąd",
               "podatk"],
    "administracja": ["administrac", "urzęd", "pracowni", "regulam", "organizac",
                      "procedur", "biurokrac"],
}


def classify_category(przedmiot):
    """Klasyfikuje kategorię interpelacji na podstawie przedmiotu."""
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
    """Główna funkcja scrapowania."""
    session = requests.Session()
    all_records = []

    for kad_name in kadencje:
        kad = KADENCJE.get(kad_name)
        if not kad:
            print(f"Nieznana kadencja: {kad_name}")
            continue

        list_id = kad.get("list_id")
        if not list_id:
            print(f"  Brak list_id dla kadencji {kad_name}, pomijam")
            continue

        print(f"\n=== {kad['label']} ===")

        page = 1
        total_pages = None
        kad_records = []

        while True:
            try:
                html = fetch_list_page(session, page, PER_PAGE, debug=debug)
                records, pages = parse_list_page(html, kad_name, debug=debug)
            except Exception as e:
                print(f"  BLAD na stronie {page}: {e}")
                break

            if total_pages is None:
                total_pages = max(pages, 1)
                print(f"  Lacznie stron: {total_pages}")

            kad_records.extend(records)

            if debug:
                print(f"  Strona {page}/{total_pages}: {len(records)} rekordow")
            elif page % 10 == 0:
                print(f"  Strona {page}/{total_pages}...")

            if not records or page >= total_pages:
                break

            page += 1
            time.sleep(DELAY)

        print(f"  Pobrano: {len(kad_records)} rekordow")

        # Optionally fetch details for each record
        if fetch_details:
            print(f"\n  Pobieram szczegóły ({len(kad_records)} rekordów)...")
            for i, rec in enumerate(kad_records):
                bip_url = rec.get("bip_url", "")
                if not bip_url:
                    continue
                detail = fetch_detail(session, bip_url, debug=debug)
                if detail:
                    rec.update({k: v for k, v in detail.items() if v})
                if (i + 1) % 50 == 0:
                    print(f"  Szczegóły: {i+1}/{len(kad_records)}")
                time.sleep(DELAY)

        all_records.extend(kad_records)

    # Classify categories and normalize fields
    for rec in all_records:
        rec["kategoria"] = classify_category(rec.get("przedmiot", ""))

        # Normalize status
        status = rec.get("status", "").lower()
        rec["odpowiedz_status"] = status

        # Clean up internal fields
        rec.pop("article_id", None)

        # Ensure consistent output fields
        rec.setdefault("data_wplywu", "")
        rec.setdefault("data_odpowiedzi", "")
        rec.setdefault("tresc_url", "")
        rec.setdefault("odpowiedz_url", "")
        rec.setdefault("nr_sprawy", "")

    # Sort by newest first (by bip_url article_id as proxy, or data_wplywu)
    all_records.sort(
        key=lambda x: x.get("data_wplywu", "") or x.get("bip_url", ""),
        reverse=True,
    )

    # Stats
    interp = sum(1 for r in all_records if r.get("typ") == "interpelacja")
    zap = sum(1 for r in all_records if r.get("typ") == "zapytanie")
    answered = sum(1 for r in all_records if "udzielono" in r.get("odpowiedz_status", ""))
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
        description="Scraper interpelacji i zapytań radnych z BIP Bydgoszcz"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--kadencja", default="IX",
        help="Kadencja: IX, VIII lub 'all' (domyślnie: IX)"
    )
    parser.add_argument(
        "--skip-details", action="store_true",
        help="Pomiń pobieranie szczegółów (szybciej, ale brak dat i załączników)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML dla detail pages."
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
