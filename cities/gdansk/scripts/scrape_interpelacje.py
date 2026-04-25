#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Gdańsk.

Źródło:
  - Interpelacje: https://bip.gdansk.pl/rada-miasta/publikacja-interpelacji-radnych-miasta-gdanska
  - Zapytania: https://bip.gdansk.pl/rada-miasta/publikacja-zapytan-radnych-miasta-gdanska

Strony BIP ładują dane AJAX-em po zmianie roku (pzrmg.changeYear).
Endpoint: POST z form-data year={rok} na tę samą stronę.

Użycie:
  python3 scrape_interpelacje.py [--lata 2024,2025,2026] [--output docs/interpelacje.json]
"""

import argparse
import json
import os
import re
import sys
import time
from html.parser import HTMLParser

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


# Stare endpointy (przed 2025): POST na /rada-miasta/... z form-data year={rok}
# Nowe endpointy (od ~2025): GET na /picklock/... z ?rok={rok}&wyszukiwana_tresc=
INTERP_URL_OLD = "https://bip.gdansk.pl/rada-miasta/publikacja-interpelacji-radnych-miasta-gdanska"
ZAP_URL_OLD = "https://bip.gdansk.pl/rada-miasta/publikacja-zapytan-radnych-miasta-gdanska"

INTERP_URL = "https://bip.gdansk.pl/picklock/publikacja-interpelacji-radnych-miasta-gdanska"
ZAP_URL = "https://bip.gdansk.pl/picklock/publikacja-zapytan-radnych-miasta-gdanska"

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://gdansk.radoskop.pl; kontakt@radoskop.pl)"
}


def parse_date(raw):
    """Konwertuje datę z formatu DD.MM.YYYY lub YYYY-MM-DD na YYYY-MM-DD.

    Akceptuje też format z czasem (YYYY-MM-DD HH:MM:SS) i zwraca samą datę.
    """
    if not raw or not raw.strip():
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw


def get_kadencja(data_wplywu):
    """Przypisuje kadencję na podstawie daty wpływu."""
    if not data_wplywu:
        return "?"
    if data_wplywu >= "2024-05":
        return "IX"
    elif data_wplywu >= "2018-11":
        return "VIII"
    elif data_wplywu >= "2014-11":
        return "VII"
    else:
        return "VI"


def fetch_year(session, base_url, year, debug=False):
    """Pobiera stronę z interpelacjami/zapytaniami dla danego roku.

    BIP Gdańsk od ~2025 udostępnia dane przez GET /picklock/...?rok={rok}
    (wcześniej: POST /rada-miasta/... z form-data year={rok}).

    Endpoint zwraca JSON: {"status": 200, "results": "<html>..."}.
    Wyciągamy HTML z pola "results".
    """
    params = {"rok": str(year), "wyszukiwana_tresc": ""}
    resp = session.get(base_url, params=params, headers=HEADERS, timeout=120)
    resp.raise_for_status()

    # Endpoint zwraca JSON z polem "results" zawierającym HTML
    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            data = resp.json()
            html = data.get("results", "")
            if debug:
                print(f"\n[DEBUG] GET {resp.url}")
                print(f"[DEBUG] JSON response, status={data.get('status')}, results length={len(html)}")
            return html
        except (ValueError, KeyError):
            pass

    # Fallback: traktuj jako surowy HTML
    if debug:
        print(f"\n[DEBUG] GET {resp.url}")
        print(f"[DEBUG] Status: {resp.status_code}, length: {len(resp.text)}")
        print(f"[DEBUG] Content-Type: {content_type}")

    return resp.text


_PUBL_DATE_RE = re.compile(
    r"Data publikacji informacji:\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def extract_publikacja_date(table):
    """Wyciąga 'Data publikacji informacji' z metryki interpelacji jeśli istnieje.

    Metryka jest w collapsed div wewnątrz komórki Uwagi:
      <span class="d-block">Data publikacji informacji: 2026-01-05 13:40:57</span>
    """
    text = table.get_text(" ", strip=True)
    m = _PUBL_DATE_RE.search(text)
    return m.group(1) if m else ""


def reconcile_dates(obj, today_iso):
    """Heurystyka naprawiająca błędne daty wpływu z BIP Gdańska.

    BIP czasem ma data_wplywu w przyszłości albo po data_odpowiedzi (literówka
    urzędnika). W takich przypadkach podmienia ją na data_publikacji z metryki,
    która zwykle pokrywa się z momentem rzeczywistego wpływu.
    """
    wpl = obj.get("data_wplywu", "")
    odp = obj.get("data_odpowiedzi", "")
    publ = obj.get("data_publikacji", "")
    if not wpl:
        return
    invalid = wpl > today_iso or (odp and wpl > odp)
    if not invalid:
        return
    if publ and publ <= today_iso and (not odp or publ <= odp):
        obj["data_wplywu_oryginal"] = wpl
        obj["data_wplywu"] = publ
        obj["rok"] = int(publ[:4])
        obj["kadencja"] = get_kadencja(publ)


def parse_tables(html, typ, url_prefix, debug=False):
    """Parsuje tabelki z interpelacjami/zapytaniami z HTML."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="table-sm")

    if debug:
        print(f"[DEBUG] Found {len(tables)} tables with class='table-sm'")
        # Show what table classes actually exist
        all_tables = soup.find_all("table")
        table_classes = set()
        for t in all_tables:
            cls = t.get("class")
            table_classes.add(str(cls))
        if table_classes and len(table_classes) <= 10:
            print(f"[DEBUG] All table classes found: {table_classes}")

    records = []
    for table_idx, table in enumerate(tables):
        obj = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            val_cell = cells[1]
            val_text = val_cell.get_text(strip=True)

            if debug and table_idx == 0:
                print(f"[DEBUG] Label found: '{label}' -> '{val_text[:50]}'")

            if "cr" in label and ("cri" in label or "crz" in label or label.startswith("cr")):
                obj["cri"] = val_text
            elif "ezd" in label:
                obj["ezd"] = val_text
            elif "data wpływu" in label or "data wplywu" in label:
                obj["data_wplywu"] = parse_date(val_text)
            elif "imię" in label or "imie" in label:
                obj["radny"] = val_text
            elif "przedmiot" in label:
                obj["przedmiot"] = val_text.replace("\n", " ").strip()
            elif label == "treść" or label == "tresc":
                a = val_cell.find("a")
                obj["tresc_url"] = a["href"] if a and a.get("href") else ""
            elif "data odpowiedzi" in label:
                obj["data_odpowiedzi"] = parse_date(val_text)
            elif "treść odpowiedzi" in label or "odpowiedz" in label:
                a = val_cell.find("a")
                obj["odpowiedz_url"] = a["href"] if a and a.get("href") else ""

        if debug and obj:
            print(f"[DEBUG] Table {table_idx} parsed object keys: {list(obj.keys())}")

        if obj.get("cri"):
            # Prefix zapytania with Z if not already
            if typ == "zapytanie" and not obj["cri"].startswith("Z"):
                obj["cri"] = "Z" + obj["cri"]

            # Metryka publikacji (defensywny fallback dla błędnych dat z BIP)
            publ = extract_publikacja_date(table)
            if publ:
                obj["data_publikacji"] = publ

            obj.setdefault("ezd", "")
            obj.setdefault("data_wplywu", "")
            obj.setdefault("radny", "")
            obj.setdefault("przedmiot", "")
            obj.setdefault("data_odpowiedzi", "")
            obj.setdefault("tresc_url", "")
            obj.setdefault("odpowiedz_url", "")

            # Naprawa błędów BIP: jeśli wpływ jest w przyszłości albo po
            # odpowiedzi, podmień na datę publikacji z metryki.
            from datetime import date
            reconcile_dates(obj, date.today().isoformat())

            rok = int(obj["data_wplywu"][:4]) if obj["data_wplywu"] else 0
            obj["rok"] = rok
            obj["typ"] = typ
            obj["kadencja"] = get_kadencja(obj["data_wplywu"])

            records.append(obj)

    return records


def scrape(years, output_path, debug=False):
    """Główna funkcja scrapowania."""
    session = requests.Session()
    all_records = []

    # Interpelacje
    print("=== Interpelacje ===")
    for year in years:
        print(f"  Rok {year}...", end=" ", flush=True)
        try:
            html = fetch_year(session, INTERP_URL, year, debug=debug)
            records = parse_tables(html, "interpelacja", "rmg_i", debug=debug)
            print(f"{len(records)} rekordów")
            all_records.extend(records)
        except Exception as e:
            print(f"BŁĄD: {e}")
            if debug:
                import traceback
                traceback.print_exc()
        time.sleep(1)

    # Zapytania
    print("\n=== Zapytania ===")
    for year in years:
        print(f"  Rok {year}...", end=" ", flush=True)
        try:
            html = fetch_year(session, ZAP_URL, year, debug=debug)
            records = parse_tables(html, "zapytanie", "rmg_p", debug=debug)
            print(f"{len(records)} rekordów")
            all_records.extend(records)
        except Exception as e:
            print(f"BŁĄD: {e}")
            if debug:
                import traceback
                traceback.print_exc()
        time.sleep(1)

    # Sortuj od najnowszych
    all_records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Statystyki
    interp_count = sum(1 for r in all_records if r["typ"] == "interpelacja")
    zap_count = sum(1 for r in all_records if r["typ"] == "zapytanie")
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp_count}")
    print(f"Zapytania:    {zap_count}")
    print(f"Razem:        {len(all_records)}")

    # Zapisz
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.0f} KB)")


def main():
    parser = argparse.ArgumentParser(description="Scraper interpelacji i zapytań z BIP Gdańsk")
    parser.add_argument(
        "--lata", default="2017,2018,2019,2020,2021,2022,2023,2024,2025,2026",
        help="Lata do scrapowania (oddzielone przecinkami)"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie debugowania"
    )
    args = parser.parse_args()

    years = [int(y.strip()) for y in args.lata.split(",")]
    scrape(years, args.output, debug=args.debug)


if __name__ == "__main__":
    main()
