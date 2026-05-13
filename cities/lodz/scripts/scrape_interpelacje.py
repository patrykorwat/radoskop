#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Łódź.

Źródło: https://bip.uml.lodz.pl/wladze/rada-miejska-w-lodzi/interpelacje-i-zapytania-radnych/

Lista: tabele <th>label</th><td>value</td>
Detail: pełna strona interpelacji — daty, PDF-y (treść + odpowiedź)

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny

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

BASE_URL = "https://bip.uml.lodz.pl"

KADENCJE = {
    "IX":   {"label": "IX kadencja (2024–2029)"},
    "VIII": {"label": "VIII kadencja (2018–2024)"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://lodz.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html",
}

DELAY = 0.5
PER_PAGE = 25


# ---------------------------------------------------------------------------
# Scraping — list page
# ---------------------------------------------------------------------------

def fetch_page(session, url, debug=False):
    """Pobiera stronę."""
    if debug:
        print(f"  [DEBUG] GET {url}")

    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_list_page(html, kadencja_name, debug=False):
    """Parsuje stronę listy interpelacji.

    Każda interpelacja to osobna <table> z wierszami <th> + <td>:
      - Przedmiot/Temat: <link> do szczegółów
      - Radny: imię i nazwisko
      - Status: oczekuje / udzielono

    Diagnostic logging: jeśli BIP zmieni format z tabel na cardy/divy,
    pomoże szybciej namierzyć regresję. Log pokazuje ile <table> znaleziono
    plus etykietki <th> z pierwszej tabeli, żeby porównać z aktualną
    strukturą strony.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    tables = main.find_all("table")

    # Diagnostic: liczba tabel i pierwsze etykiety. Aktualny scraper zakłada
    # że każda interpelacja jest w osobnej <table>; jeśli BIP przeszedł
    # na inny markup (cardy, listy, divy), tables=0 lub etykietki nie
    # zawierają "przedmiot/temat/sprawie" → 0 records mimo że strony są.
    if not tables:
        # Fallback: spróbuj znaleźć interpelacje w innych strukturach.
        # Logujemy znaleziska żeby user mógł zaproponować selektor.
        articles = main.find_all("article")
        cards = main.find_all("div", class_=re.compile(r"card|item|interpelacj", re.I))
        list_items = main.find_all("li", class_=re.compile(r"item|entry|interpelacj", re.I))
        print(f"  [parse] BRAK tabel. Diagnostyka: {len(articles)} <article>, "
              f"{len(cards)} <div class~=card/item>, {len(list_items)} <li class~=item>")
        if debug:
            print(f"  [DEBUG] HTML excerpt main (pierwsze 1500 znakow):")
            print(str(main)[:1500])
    elif debug or True:
        first_labels = []
        for row in tables[0].find_all("tr")[:5]:
            th = row.find("th")
            if th:
                first_labels.append(th.get_text(strip=True))
        print(f"  [parse] {len(tables)} <table> znaleziono. Etykiety w pierwszej: {first_labels[:5]}")

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

            # Subject — check for link
            if "przedmiot" in label or "temat" in label or "sprawie" in label:
                a = td.find("a")
                if a:
                    record["przedmiot"] = a.get_text(strip=True)
                    href = a.get("href", "")
                    if href.startswith("/"):
                        record["bip_url"] = BASE_URL + href
                    elif href.startswith("http"):
                        record["bip_url"] = href
                    # Extract article_id
                    m = re.search(r'/([a-z0-9\-]+)/?$', href)
                    if m:
                        record["article_id"] = m.group(1)
                else:
                    record["przedmiot"] = val_text

                # Determine type
                if "zapytanie" in label or "zapytań" in label:
                    record["typ"] = "zapytanie"
                elif "wniosek" in label:
                    record["typ"] = "wniosek"
                else:
                    record["typ"] = "interpelacja"

            elif "radny" in label or "radnej" in label or "tożsamość" in label:
                record["radny"] = val_text

            elif "status" in label or "odpowiedź" in label:
                record["status"] = val_text

        if record.get("przedmiot"):
            record.setdefault("radny", "")
            record.setdefault("status", "")
            record.setdefault("bip_url", "")
            record.setdefault("article_id", "")
            record.setdefault("typ", "interpelacja")
            record["kadencja"] = kadencja_name
            records.append(record)

    # Extract total pages from pagination
    total_pages = 1
    for a in soup.find_all("a"):
        href = a.get("href", "")
        m = re.search(r'[?&]page=(\d+)', href)
        if m:
            p = int(m.group(1))
            if p > total_pages:
                total_pages = p
    # Also check for numbered page links
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

def fetch_detail(session, bip_url, debug=False):
    """Pobiera szczegóły interpelacji z jej strony."""
    if not bip_url:
        return {}

    if debug:
        print(f"  [DEBUG] GET {bip_url}")

    try:
        resp = session.get(bip_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        detail = {}
        # Parse table rows (th + td pairs)
        for row in soup.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue

            label = th.get_text(strip=True).lower()
            val = td.get_text(strip=True)

            if "typ" in label and "wyst" in label:
                detail["typ_full"] = val
            elif "nr" in label and ("sprawy" in label or "numer" in label):
                detail["nr_sprawy"] = val
            elif "data" in label and ("wpływ" in label or "wplyw" in label or "wytwor" in label):
                detail["data_wplywu"] = parse_date(val)
            elif "data" in label and "odpowied" in label:
                detail["data_odpowiedzi"] = parse_date(val)

        # Find attachment links
        attachments = []
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if ".pdf" in href.lower() or "zalacznik" in href.lower() or "attach" in href.lower():
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

        print(f"\n=== {kad['label']} ===")

        # BIP Łódź URL — main interpelacje list page
        list_url = f"{BASE_URL}/wladze/rada-miejska-w-lodzi/interpelacje-i-zapytania-radnych/"

        page = 1
        total_pages = None
        kad_records = []

        while True:
            try:
                # Łódź BIP might not have pagination, but we support it
                page_url = list_url
                if page > 1:
                    page_url = f"{list_url}?page={page}"

                html = fetch_page(session, page_url, debug=debug)
                records, pages = parse_list_page(html, kad_name, debug=debug)
            except Exception as e:
                print(f"  BŁĄD na stronie {page}: {e}")
                break

            if total_pages is None:
                total_pages = max(pages, 1)
                print(f"  Łącznie stron: {total_pages}")

            kad_records.extend(records)

            if debug:
                print(f"  Strona {page}/{total_pages}: {len(records)} rekordów")
            elif page % 10 == 0:
                print(f"  Strona {page}/{total_pages}...")

            if not records or page >= total_pages:
                break

            page += 1
            time.sleep(DELAY)

        print(f"  Pobrano: {len(kad_records)} rekordów")

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

    # Sort by newest first
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
        description="Scraper interpelacji i zapytań radnych z BIP Łódź"
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
    args = parser.parse_args()

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
