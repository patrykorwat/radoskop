#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Warszawa.

Źródło: https://bip.warszawa.pl/web/rada-warszawy/interpelacje-zapytania-rada-warszawy
Platforma: Liferay — server-side rendered HTML, paginacja przez URL params.

Portlet: web_content_search_portlet_INSTANCE_lumk
Parametry:
  - _..._cur=N            — numer strony (1-indexed)
  - _..._delta=N           — ilość wyników na stronie (domyślnie 25)
  - _..._categoryIds=ID    — filtr kadencji
  - _..._orderByType=desc  — sortowanie malejąco po dacie

Kadencje (categoryIds):
  2024-2029 → 1046979
  2018-2023 → 46650
  2014-2018 → 46647
  2010-2014 → 46644
  2006-2010 → 46641
  2002-2006 → 46638

Listing zawiera pola: Numer, Data, W sprawie, Radny/a, Klub, Odpowiedź.
Typ (interpelacja/zapytanie) wyciągany z URL slugu (np. "-interpelacja-nr-" vs "-zapytanie-nr-").
Szczegóły (PDF-y) wymagają oddzielnych requestów do stron detali.

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja 2024-2029]
                                 [--no-fetch-details]
                                 [--debug]

Domyślnie pobiera szczegóły każdej interpelacji (PDF-y, data odpowiedzi).
Użyj --no-fetch-details dla szybszego scrape bez detali.
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

BASE_URL = "https://bip.warszawa.pl/web/rada-warszawy/interpelacje-zapytania-rada-warszawy"
PORTLET = "web_content_search_portlet_INSTANCE_lumk"
P = f"_{PORTLET}_"

KADENCJE = {
    "2024-2029": {"cat_id": "1046979", "label": "Kadencja 2024–2029"},
    "2018-2023": {"cat_id": "46650",   "label": "Kadencja 2018–2023"},
    "2014-2018": {"cat_id": "46647",   "label": "Kadencja 2014–2018"},
    "2010-2014": {"cat_id": "46644",   "label": "Kadencja 2010–2014"},
    "2006-2010": {"cat_id": "46641",   "label": "Kadencja 2006–2010"},
    "2002-2006": {"cat_id": "46638",   "label": "Kadencja 2002–2006"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://warszawa.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,application/xhtml+xml",
}

DELAY = 1.0        # sekundy między requestami
PAGE_SIZE = 25      # domyślny rozmiar strony portletu (nie zmieniamy, bo serwer może ignorować)


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def build_url(cat_id, page=1, order="desc"):
    """Buduje URL z filtrami portletu Liferay."""
    params = {
        "p_p_id": PORTLET,
        "p_p_lifecycle": "0",
        "p_p_state": "normal",
        "p_p_mode": "view",
        f"{P}mvcPath": "/view.jsp",
        f"{P}cur": str(page),
        f"{P}orderByType": order,
        f"{P}categoryIds": cat_id,
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE_URL}?{qs}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def extract_type_from_slug(slug):
    """Wyciąga typ z URL slugu: interpelacja lub zapytanie."""
    if not slug:
        return "interpelacja"
    slug_lower = slug.lower()
    if "-zapytanie-" in slug_lower or slug_lower.endswith("-zapytanie"):
        return "zapytanie"
    return "interpelacja"


def parse_listing_page(html, debug=False):
    """Parsuje stronę listingu interpelacji. Zwraca (records, total_count)."""
    soup = BeautifulSoup(html, "html.parser")

    # Liczba wyników:
    # <p class="search-total-results-wrapper">
    #   <span class="search-total-results-label">liczba wyników</span>
    #   <span class="search-total-results-value">1751</span>
    # </p>
    total = 0
    total_span = soup.find("span", class_="search-total-results-value")
    if total_span:
        try:
            total = int(total_span.get_text(strip=True).replace(" ", ""))
        except ValueError:
            pass
    else:
        # Fallback: szukamy w wrapper
        total_wrapper = soup.find(class_="search-total-results-wrapper")
        if total_wrapper:
            text = total_wrapper.get_text(strip=True)
            m = re.search(r"(\d[\d\s]*)", text)
            if m:
                try:
                    total = int(m.group(1).replace(" ", ""))
                except ValueError:
                    pass

    if debug:
        print(f"  [DEBUG] Total count on page: {total}")

    # Lista wyników: <ul class="search-result-list ...">
    results_list = soup.find("ul", class_="search-result-list")
    if not results_list:
        # Fallback: szukamy heading "Znalezione interpelacje"
        results_heading = soup.find(string=re.compile(r"Znalezione interpelacje"))
        if results_heading:
            results_list = results_heading.find_parent().find_next_sibling("ul")

    if not results_list:
        if debug:
            print("  [DEBUG] Nie znaleziono listy wyników")
        return [], total

    records = []
    items = results_list.find_all("li", recursive=False)

    if debug:
        print(f"  [DEBUG] Znaleziono {len(items)} elementów listy")

    for item in items:
        link = item.find("a", class_="search-entry-link-wrapper")
        if not link:
            link = item.find("a")
        if not link:
            continue

        href = link.get("href", "")
        slug = href.rsplit("/", 1)[-1] if href else ""

        # Parsuj pola: <p class="search-entry-data-label"> → następny <p> = wartość
        ps = link.find_all("p")
        fields = {}
        current_label = None

        for p in ps:
            text = p.get_text(strip=True)
            if not text:
                continue

            cls = p.get("class", [])
            cls_str = " ".join(cls) if isinstance(cls, list) else str(cls)

            if "search-entry-data-label" in cls_str:
                current_label = text.lower()
            elif current_label:
                fields[current_label] = text
                current_label = None

        numer = fields.get("numer", "")
        data = fields.get("data", "")
        przedmiot = fields.get("w sprawie", "")
        radny_raw = fields.get("radny/a", "")
        # Usuń oznaczenie klubu w nawiasie, np. "Mazurek Piotr (pis)" → "Mazurek Piotr"
        radny = re.sub(r'\s*\([^)]*\)\s*$', '', radny_raw).strip()
        klub = fields.get("klub", "")
        odpowiedz_status = fields.get("odpowiedź", fields.get("odpowiedz", ""))

        typ = extract_type_from_slug(slug)

        if not numer:
            if debug:
                print(f"  [DEBUG] Pominięto element bez numeru: {slug}")
            continue

        # CRI format
        cri = numer
        if typ == "zapytanie":
            cri = f"Z{numer}"

        rok = int(data[:4]) if data and len(data) >= 4 else 0

        record = {
            "cri": cri,
            "typ": typ,
            "rok": rok,
            "kadencja": "",  # uzupełniane niżej
            "radny": radny,
            "przedmiot": przedmiot,
            "data_wplywu": data,
            "klub": klub,
            "odpowiedz_status": odpowiedz_status,
            "tresc_url": "",
            "odpowiedz_url": "",
            "data_odpowiedzi": "",
            "detail_path": href,
        }
        records.append(record)

    return records, total


def parse_detail_page(html, debug=False):
    """Parsuje stronę szczegółów interpelacji. Zwraca dict z dodatkowymi polami."""
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    # Etykiety: <p class="h5-style bip-data-subtitle">Label</p>
    # Wartości: następny sibling <p> lub <div>
    labels = soup.find_all("p", class_="bip-data-subtitle")
    for label_el in labels:
        label_text = label_el.get_text(strip=True).lower()
        next_el = label_el.find_next_sibling()
        if not next_el:
            continue
        value = next_el.get_text(strip=True)

        if "interpelacja/zapytanie" in label_text and value.lower() in ("interpelacja", "zapytanie"):
            result["typ_detail"] = value.lower()
        elif label_text == "kadencja":
            result["kadencja"] = value

    # Pliki do pobrania — linki do /documents/...
    file_links = soup.find_all("a", href=re.compile(r"/documents/"))
    tresc_found = False
    for link in file_links:
        href = link.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://bip.warszawa.pl{href}"

        # Nazwa pliku w kontekście (sibling text lub parent text)
        context = ""
        parent = link.parent
        if parent:
            context = parent.get_text(strip=True).lower()

        # Sekcja "Odpowiedź" vs "Interpelacja/zapytanie"
        # Szukamy nagłówka sekcji nadrzędnej
        section_title = ""
        for ancestor in link.parents:
            title_el = ancestor.find_previous_sibling("p", class_="bip-data-subtitle")
            if title_el:
                section_title = title_el.get_text(strip=True).lower()
                break

        if "odpowied" in context or "odpowied" in section_title:
            if not result.get("odpowiedz_url"):
                result["odpowiedz_url"] = href
                # Próbuj wyciągnąć datę odpowiedzi z nazwy pliku: *_odp_DD_MM_YYYY_*
                fname = href.rsplit("/", 1)[-1] if "/" in href else href
                m = re.search(r"_odp_(\d{2})_(\d{2})_(\d{4})_", fname)
                if m:
                    result["data_odpowiedzi"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        elif not tresc_found:
            result["tresc_url"] = href
            tresc_found = True

    return result


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# Import shared HTTP cache (sys.path bo scraper pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_text, init_cache  # noqa: E402


def fetch_page(session, url, debug=False, force: bool = False):
    """Pobiera stronę HTML. Cache aktywny gdy --cache-dir przekazany.

    force=True dla list paginacji (musi wykrywać nowe interpelacje).
    Detail pages cache'ują się domyślnie (treść interpelacji finalna).
    """
    if debug:
        print(f"  [DEBUG] GET {url[:120]}...")
    return cached_fetch_text(url, session=session, headers=HEADERS, delay=0.0, timeout=60, force=force)


def scrape_kadencja(session, kad_id, kad_label, cat_id, fetch_details=False, debug=False):
    """Scrapuje wszystkie interpelacje z danej kadencji."""
    print(f"\n=== {kad_label} (categoryId={cat_id}) ===")

    records = []
    page = 1
    total = None

    while True:
        url = build_url(cat_id, page=page)
        try:
            # Lista zawsze fresh: chcemy wykryć nowe interpelacje.
            html = fetch_page(session, url, debug=debug, force=True)
        except Exception as e:
            print(f"  BŁĄD strony {page}: {e}")
            break

        page_records, page_total = parse_listing_page(html, debug=(debug and page == 1))

        if total is None:
            total = page_total
            print(f"  Łącznie wyników: {total}")

        if not page_records:
            if debug:
                print(f"  [DEBUG] Strona {page}: brak rekordów, kończę")
            break

        # Uzupełnij kadencję
        for r in page_records:
            r["kadencja"] = kad_id

        records.extend(page_records)
        print(f"  Strona {page}: {len(page_records)} rekordów (razem: {len(records)}/{total})")

        if len(records) >= total:
            break

        page += 1
        time.sleep(DELAY)

    # Opcjonalnie pobierz szczegóły (PDF-y, typ, kadencja)
    if fetch_details and records:
        print(f"  Pobieram szczegóły {len(records)} interpelacji...")
        for i, rec in enumerate(records):
            detail_path = rec.get("detail_path", "")
            if not detail_path:
                continue

            detail_url = f"https://bip.warszawa.pl{detail_path}" if detail_path.startswith("/") else detail_path

            try:
                detail_html = fetch_page(session, detail_url, debug=False)
                detail = parse_detail_page(detail_html, debug=debug)

                if detail.get("tresc_url"):
                    rec["tresc_url"] = detail["tresc_url"]
                if detail.get("odpowiedz_url"):
                    rec["odpowiedz_url"] = detail["odpowiedz_url"]
                if detail.get("data_odpowiedzi"):
                    rec["data_odpowiedzi"] = detail["data_odpowiedzi"]
                if detail.get("kadencja") and not rec.get("kadencja"):
                    rec["kadencja"] = detail["kadencja"]
                if detail.get("typ_detail"):
                    rec["typ"] = detail["typ_detail"]
                    # Popraw CRI jeśli typ się zmienił
                    numer = rec["cri"].lstrip("Z")
                    rec["cri"] = f"Z{numer}" if rec["typ"] == "zapytanie" else numer

            except Exception as e:
                if debug:
                    print(f"  [DEBUG] Błąd szczegółów {detail_path}: {e}")

            if (i + 1) % 50 == 0:
                print(f"  Szczegóły: {i+1}/{len(records)}")

            time.sleep(DELAY * 0.5)

    return records


def init_session(session, debug=False):
    """Inicjalizuje sesję Liferay — pobiera cookies wymagane do dostępu do portletu."""
    print("Inicjalizacja sesji Liferay...")
    url = BASE_URL
    if debug:
        print(f"  [DEBUG] GET {url}")
    resp = session.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    cookies = list(session.cookies.keys())
    if debug:
        print(f"  [DEBUG] Status: {resp.status_code}, cookies: {cookies}, HTML len: {len(resp.text)}")
    if not cookies:
        print("  UWAGA: Nie otrzymano cookies sesji — kolejne requesty mogą nie działać")
    else:
        print(f"  Sesja OK (cookies: {', '.join(cookies)})")
    time.sleep(DELAY)


def scrape(kadencje, output_path, fetch_details=False, debug=False):
    """Główna funkcja scrapowania."""
    session = requests.Session()

    # Liferay wymaga aktywnej sesji (cookies) żeby zwracać treść portletu
    init_session(session, debug=debug)

    all_records = []

    for kad_id in kadencje:
        kad = KADENCJE.get(kad_id)
        if not kad:
            print(f"Nieznana kadencja: {kad_id}")
            continue

        records = scrape_kadencja(
            session, kad_id, kad["label"], kad["cat_id"],
            fetch_details=fetch_details, debug=debug,
        )
        all_records.extend(records)

    # Zamień detail_path na pełny URL BIP
    for r in all_records:
        dp = r.pop("detail_path", "")
        if dp:
            r["bip_url"] = f"https://bip.warszawa.pl{dp}" if dp.startswith("/") else dp

    # Sortuj od najnowszych
    all_records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Statystyki
    interp = sum(1 for r in all_records if r["typ"] == "interpelacja")
    zap = sum(1 for r in all_records if r["typ"] == "zapytanie")
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Razem:        {len(all_records)}")

    # Zapisz
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Warszawa"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--kadencja", default="2024-2029",
        help="Kadencja: '2024-2029', '2018-2023', ... lub 'all' (domyślnie: 2024-2029)"
    )
    parser.add_argument(
        "--no-fetch-details", action="store_true",
        help="Pomiń pobieranie szczegółów (szybszy scrape, ale bez PDF-ów i dat odpowiedzi)"
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

    if args.kadencja == "all":
        kadencje = list(KADENCJE.keys())
    else:
        kadencje = [k.strip() for k in args.kadencja.split(",")]

    scrape(
        kadencje=kadencje,
        output_path=args.output,
        fetch_details=not args.no_fetch_details,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
