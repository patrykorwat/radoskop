#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Lublin.

Źródło: https://bip.lublin.eu/rada-miasta-lublin/

Struktura BIP Lublin (stan: kwiecień 2026):
  Lista interpelacji:
    /rada-miasta-lublin/ix-kadencja/interpelacje-i-zapytania-radnych/
  Paginacja:
    /rada-miasta-lublin/ix-kadencja/interpelacje-i-zapytania-radnych/{N},strona.html
  Strona szczegółów zawiera:
    - h1: tytuł interpelacji
    - div.label/div.value: Kadencja, Rodzaj dokumentu, Nr dokumentu, Data wpływu, Data odpowiedzi
    - div.form-row "Odpowiedzialny za treść informacji: Imię Nazwisko - Radny Rady Miasta Lublin"
    - linki do PDF (treść interpelacji + odpowiedź)

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

BASE_URL = "https://bip.lublin.eu"

KADENCJE = {
    "IX": {
        "label": "IX kadencja (2024–2029)",
        "list_path": "/rada-miasta-lublin/ix-kadencja/interpelacje-i-zapytania-radnych/",
    },
    "VIII": {
        "label": "VIII kadencja (2018–2024)",
        "list_path": "/rada-miasta-lublin/viii-kadencja/interpelacje-i-zapytania-radnych/",
    },
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://lublin.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html",
}

DELAY = 0.4


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

# Import shared HTTP cache (sys.path bo scraper pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_text, init_cache  # noqa: E402


def fetch_page(session, url, debug=False, force: bool = False):
    """Pobiera stronę HTML. Cache aktywny gdy --cache-dir przekazany.

    force=True dla listy stron (paginacja może mieć nowe interpelacje).
    """
    if debug:
        print(f"  [DEBUG] GET {url}")
    return cached_fetch_text(url, session=session, headers=HEADERS, delay=0.1, force=force)


def discover_page_count(soup):
    """Finds the last page number from pagination links."""
    max_page = 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/(\d+),strona\.html", href)
        if m:
            n = int(m.group(1))
            if n > max_page:
                max_page = n
    return max_page


def parse_list_page(soup, base_url):
    """Parsuje stronę listy interpelacji. Zwraca listę (title, detail_url)."""
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        text_lower = text.lower()

        if not ("interpelacja" in text_lower or "zapytanie" in text_lower
                or "wniosek" in text_lower):
            continue

        if not href.startswith("http"):
            href = f"{base_url}{href}" if href.startswith("/") else f"{base_url}/{href}"

        if href in seen:
            continue
        seen.add(href)

        typ = "interpelacja"
        if "zapytanie" in text_lower:
            typ = "zapytanie"
        elif "wniosek" in text_lower:
            typ = "wniosek"

        results.append({"przedmiot": text, "bip_url": href, "typ": typ})
    return results


def parse_date(raw):
    """Konwertuje datę na format YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    return raw


def extract_radny(soup):
    """Wyciąga nazwisko radnego ze strony szczegółów.

    BIP Lublin trzyma radnego w div.form-row:
      "Odpowiedzialny za treść informacji: Imię Nazwisko - Radny Rady Miasta Lublin"
    lub
      "Wytworzył informację: Imię Nazwisko - Radny Rady Miasta Lublin"
    """
    for div in soup.find_all("div", class_="form-row"):
        text = div.get_text(strip=True)
        for prefix in ["Odpowiedzialny za treść informacji:", "Wytworzył informację:"]:
            if prefix in text:
                after = text.split(prefix, 1)[1].strip()
                # Remove role suffix: "- Radny Rady Miasta Lublin"
                after = re.sub(r"\s*-\s*Radn[ya]\s+Rady Miasta.*$", "", after, flags=re.IGNORECASE)
                return after.strip()
    return ""


def fetch_detail(session, bip_url, debug=False):
    """Pobiera szczegóły interpelacji ze strony detalu."""
    if not bip_url:
        return {}

    try:
        html = fetch_page(session, bip_url, debug=debug)
        soup = BeautifulSoup(html, "html.parser")
        detail = {}

        # label/value pairs
        for label_div in soup.find_all(class_="label"):
            value_div = label_div.find_next_sibling(class_="value")
            if not value_div:
                continue
            label = label_div.get_text(strip=True).lower()
            val = value_div.get_text(strip=True)

            if "rodzaj" in label:
                detail["typ_full"] = val
            elif "nr" in label or "numer" in label:
                detail["nr_sprawy"] = val
            elif "data wpływu" in label:
                detail["data_wplywu"] = parse_date(val)
            elif "data odpowiedzi" in label:
                detail["data_odpowiedzi"] = parse_date(val)

        # Radny
        radny = extract_radny(soup)
        if radny:
            detail["radny"] = radny

        # PDF attachments
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" not in href.lower():
                continue
            full = f"{BASE_URL}{href}" if href.startswith("/") else href
            name = a.get_text(strip=True).lower()
            if "odp" in name and not detail.get("odpowiedz_url"):
                detail["odpowiedz_url"] = full
            elif not detail.get("tresc_url"):
                detail["tresc_url"] = full

        return detail
    except Exception as e:
        if debug:
            print(f"  [DEBUG] Error: {bip_url}: {e}")
        return {}


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
    """Główna funkcja scrapowania interpelacji."""
    session = requests.Session()
    all_records = []

    for kad_name in kadencje:
        kad = KADENCJE.get(kad_name)
        if not kad:
            print(f"Nieznana kadencja: {kad_name}")
            continue

        list_url = f"{BASE_URL}{kad['list_path']}"
        print(f"\n=== {kad['label']} ===")
        print(f"  URL: {list_url}")

        # Page 1 - lista zawsze fresh żeby wykryć nowe interpelacje.
        try:
            html = fetch_page(session, list_url, debug=debug, force=True)
        except Exception as e:
            print(f"  BŁĄD pobierania strony listy: {e}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        max_page = discover_page_count(soup)
        print(f"  Stron: {max_page}")

        records = parse_list_page(soup, BASE_URL)
        print(f"  Strona 1: {len(records)} rekordów")
        all_records.extend(records)

        # Remaining pages - też fresh
        for page in range(2, max_page + 1):
            page_url = f"{list_url}{page},strona.html"
            try:
                html = fetch_page(session, page_url, debug=debug, force=True)
                soup = BeautifulSoup(html, "html.parser")
                page_records = parse_list_page(soup, BASE_URL)
                print(f"  Strona {page}: {len(page_records)} rekordów")
                all_records.extend(page_records)
                time.sleep(DELAY)
            except Exception as e:
                print(f"  BŁĄD strona {page}: {e}")

    print(f"\nPobrano: {len(all_records)} rekordów z listy")

    # Fetch details
    if fetch_details and all_records:
        print(f"\nPobieram szczegóły ({len(all_records)} rekordów)...")
        for i, rec in enumerate(all_records):
            detail = fetch_detail(session, rec.get("bip_url", ""), debug=debug)
            if detail:
                rec.update({k: v for k, v in detail.items() if v})
            if (i + 1) % 50 == 0:
                print(f"  Szczegóły: {i+1}/{len(all_records)}")
            time.sleep(0.3)

    # Normalize and classify
    for rec in all_records:
        rec["kategoria"] = classify_category(rec.get("przedmiot", ""))
        rec["kadencja"] = kad_name

        # Determine odpowiedz_status
        if rec.get("odpowiedz_url") or rec.get("data_odpowiedzi"):
            rec["odpowiedz_status"] = "udzielono odpowiedzi"
        else:
            rec["odpowiedz_status"] = "oczekuje na odpowiedź"

        # Ensure consistent output fields
        rec.setdefault("radny", "")
        rec.setdefault("data_wplywu", "")
        rec.setdefault("data_odpowiedzi", "")
        rec.setdefault("tresc_url", "")
        rec.setdefault("odpowiedz_url", "")
        rec.setdefault("nr_sprawy", "")

    # Sort by newest first
    all_records.sort(
        key=lambda x: x.get("data_wplywu", "") or "0000",
        reverse=True,
    )

    # Stats
    interp = sum(1 for r in all_records if r.get("typ") == "interpelacja")
    zap = sum(1 for r in all_records if r.get("typ") == "zapytanie")
    answered = sum(1 for r in all_records if r.get("data_odpowiedzi"))
    radni = len(set(r.get("radny", "") for r in all_records if r.get("radny")))
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Radnych:      {radni}")
    print(f"Razem:        {len(all_records)}")

    # Save (refuse to overwrite existing data with empty result)
    if not all_records and os.path.exists(output_path):
        existing_size = os.path.getsize(output_path)
        if existing_size > 10:
            print(f"\nUWAGA: scraper zwrócił 0 wyników, zachowuję stare dane ({existing_size / 1024:.1f} KB)")
            return

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Lublin"
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
        help="Pomiń pobieranie szczegółów (szybciej, ale brak dat i radnych)"
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
