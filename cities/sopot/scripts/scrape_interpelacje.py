#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Sopot.

Źródło: https://bip.sopot.pl/ (platforma SIDAS BIP — REST API w JSON)

Endpointy:
  - Lista artykułów:  GET /api/menu/{menuId}/articles?limit=50&offset=0&archived=0
  - Szczegóły:        GET /api/articles/{articleId}
  - Plik PDF:         GET /api/files/{fileId}

Menu IDs:
  - 289 → Kadencja 2024-2029
  - 288 → Kadencja 2018-2024

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja 2024-2029]
                                 [--download-pdfs] [--pdf-dir pdfs/interpelacje]
                                 [--debug]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("Wymagany moduł: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BIP_API = "https://bip.sopot.pl/api"

KADENCJE = {
    "2024-2029": {"menu_id": 289, "label": "IX kadencja (2024–2029)"},
    "2018-2024": {"menu_id": 288, "label": "VIII kadencja (2018–2024)"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://sopot.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "application/json",
}

DELAY = 0.5
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

# Import shared HTTP cache (sys.path bo scraper pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
from http_cache import cached_fetch_json, init_cache  # noqa: E402


def api_get(session, path, params=None, debug=False, force: bool = False):
    """GET z BIP API, zwraca JSON. Z disk cache gdy --cache-dir aktywny.

    force=True dla list paginacji artykułów (musi wykrywać nowe).
    Detail endpointy /articles/{id} cache'ują się (immutable po publikacji).
    """
    url = f"{BIP_API}{path}"
    if debug:
        print(f"  [DEBUG] GET {url} params={params}")
    return cached_fetch_json(
        url, session=session, params=params, headers=HEADERS, delay=0.0, force=force,
    )


def fetch_articles(session, menu_id, debug=False):
    """Pobiera wszystkie artykuły z danego menu (paginacja)."""
    articles = []
    offset = 0

    while True:
        params = {"limit": PAGE_SIZE, "offset": offset, "archived": 0}
        # Lista paginacji ZAWSZE fresh - musimy wykryć nowe interpelacje.
        data = api_get(session, f"/menu/{menu_id}/articles", params, debug=debug, force=True)

        batch = data.get("articles", [])
        if not batch:
            break

        articles.extend(batch)
        total = data.get("total", 0)

        if debug:
            print(f"  [DEBUG] Pobrano {len(articles)}/{total} artykułów")

        if len(articles) >= total:
            break

        offset += PAGE_SIZE
        time.sleep(DELAY)

    return articles


def fetch_article_detail(session, article_id, debug=False):
    """Pobiera szczegóły artykułu (z załącznikami). Cache aktywny dla detail."""
    return api_get(session, f"/articles/{article_id}", debug=debug)


# ---------------------------------------------------------------------------
# Parsowanie
# ---------------------------------------------------------------------------

def get_alias_field(article, alias):
    """Wyciąga pole z aliasFields po nazwie aliasu."""
    for field in article.get("aliasFields", []):
        if field.get("alias") == alias:
            return field.get("value", "").strip()
    return ""


def get_column_field(article, field_id):
    """Wyciąga pole z columnFields po fieldId."""
    for field in article.get("columnFields", []):
        if field.get("fieldId") == field_id:
            return field.get("value", "").strip()
    return ""


def parse_title(title):
    """
    Parsuje tytuł artykułu.
    Przykłady:
      "Interpelacja Nr 236/2026"
      "Zapytanie Nr 235/2026"
      "Interpelacja Nr 12/2024"
    Zwraca (typ, numer, rok) lub (None, None, None).
    """
    m = re.match(
        r"(Interpelacja|Zapytanie)\s+Nr\s+(\d+)[./](\d{4})",
        title, re.IGNORECASE
    )
    if m:
        typ = m.group(1).lower()
        numer = int(m.group(2))
        rok = int(m.group(3))
        return typ, numer, rok
    return None, None, None


def parse_lead(lead):
    """
    Parsuje pole 'lead' zawierające radnego i przedmiot.
    Przykłady:
      "Interpelacja Radnego: Jakub Świderski\ndot. awarii systemu..."
      "Zapytanie Radnej: Anna Kowalska\nw sprawie remontu..."
    Zwraca (radny, przedmiot).
    """
    radny = ""
    przedmiot = ""

    # Radny
    m = re.match(
        r"(?:Interpelacja|Zapytanie)\s+Radn\w+:\s*(.+?)(?:\n|$)",
        lead, re.IGNORECASE
    )
    if m:
        radny = m.group(1).strip()

    # Przedmiot — wszystko po pierwszym newline
    parts = lead.split("\n", 1)
    if len(parts) > 1:
        przedmiot = parts[1].strip()
    elif not m:
        # Cały lead to przedmiot (brak wzorca radnego)
        przedmiot = lead.strip()

    return radny, przedmiot


def parse_publication_date(article):
    """Wyciąga datę publikacji z columnFields (fieldId=26) lub z daty artykułu."""
    # fieldId 26 = data publikacji w BIP Sopot
    date_str = get_column_field(article, 26)
    if date_str:
        # Format: "2026-03-10 08:49:43" → "2026-03-10"
        return date_str[:10]

    # Fallback: publicationDate z artykułu
    pub = article.get("publicationDate", "")
    if pub:
        return pub[:10]

    return ""


def article_to_record(article, kadencja_id):
    """Konwertuje artykuł API na rekord interpelacji."""
    title = get_alias_field(article, "title")
    lead = get_alias_field(article, "lead")

    typ, numer, rok = parse_title(title)
    radny, przedmiot = parse_lead(lead)

    data_pub = parse_publication_date(article)

    # CRI w formacie "NR/ROK"
    cri = ""
    if numer and rok:
        cri = f"{numer}/{rok}"
        if typ == "zapytanie":
            cri = f"Z{cri}"

    record = {
        "cri": cri,
        "typ": typ or "interpelacja",
        "rok": rok or 0,
        "kadencja": kadencja_id,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_pub,
        "article_id": article.get("id", 0),
        "tresc_url": "",
        "odpowiedz_url": "",
        "data_odpowiedzi": "",
    }

    return record


# ---------------------------------------------------------------------------
# Pobieranie załączników (opcjonalne)
# ---------------------------------------------------------------------------

def enrich_with_attachments(session, records, download_pdfs=False, pdf_dir=None, debug=False):
    """Pobiera szczegóły artykułów, uzupełnia URLe załączników i opcjonalnie pobiera PDF-y.

    Parallelized z ThreadPoolExecutor (4 workers default, override przez env
    SOPOT_INTERP_WORKERS). 583 detail × ~2s sekwencyjnie = 20min, z 4 workers
    ~5min. requests.Session jest thread-safe dla GET requestów (urllib3 connection
    pool synchronizuje dostęp), więc bez locków.

    Pobieranie PDF-ów (download_pdfs=True) zostaje sequential żeby nie zalewać
    BIP-u dużymi binarnymi pobraniami z N parallel connections; pipeline domyślnie
    nie pobiera PDF (download_pdfs=False).
    """
    if pdf_dir:
        os.makedirs(pdf_dir, exist_ok=True)

    max_workers = max(1, int(os.environ.get("SOPOT_INTERP_WORKERS", "4")))

    def _process_one(rec):
        article_id = rec.get("article_id")
        if not article_id:
            return rec
        try:
            detail = fetch_article_detail(session, article_id, debug=False)
        except Exception as e:
            print(f"  BŁĄD pobierania artykułu {article_id}: {e}")
            return rec

        for att in detail.get("attachments", []):
            file_id = att.get("id")
            name = att.get("name", "").lower()
            ext = att.get("extension", "").lower()
            url = f"{BIP_API}/files/{file_id}" if file_id else ""
            if "odpowied" in name or "answer" in name:
                rec["odpowiedz_url"] = url
            elif not rec["tresc_url"]:
                rec["tresc_url"] = url
            if download_pdfs and pdf_dir and ext == "pdf" and file_id:
                pdf_path = os.path.join(pdf_dir, f"{rec['cri'].replace('/', '_')}_{att.get('name', 'file')}.{ext}")
                if not os.path.exists(pdf_path):
                    try:
                        resp = session.get(url, headers=HEADERS, timeout=60)
                        resp.raise_for_status()
                        with open(pdf_path, "wb") as f:
                            f.write(resp.content)
                        if debug:
                            print(f"  [DEBUG] Pobrano: {pdf_path}")
                    except Exception as e:
                        print(f"  BŁĄD pobierania PDF {file_id}: {e}")
        return rec

    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_process_one, rec) for rec in records]
        for fut in as_completed(futures):
            done += 1
            if done % 20 == 0:
                print(f"  Szczegóły: {done}/{len(records)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(kadencje, output_path, download_pdfs=False, pdf_dir=None, debug=False):
    """Główna funkcja scrapowania."""
    session = requests.Session()
    all_records = []

    for kad_id in kadencje:
        kad = KADENCJE.get(kad_id)
        if not kad:
            print(f"Nieznana kadencja: {kad_id}")
            continue

        menu_id = kad["menu_id"]
        print(f"\n=== {kad['label']} (menu={menu_id}) ===")

        articles = fetch_articles(session, menu_id, debug=debug)
        print(f"  Pobrano {len(articles)} artykułów")

        records = []
        skipped = 0
        for art in articles:
            rec = article_to_record(art, kad_id)
            if rec["cri"]:
                records.append(rec)
            else:
                skipped += 1
                if debug:
                    title = get_alias_field(art, "title")
                    print(f"  [DEBUG] Pominięto (brak CRI): {title}")

        print(f"  Sparsowano: {len(records)} rekordów ({skipped} pominiętych)")

        # Opcjonalnie pobierz szczegóły z załącznikami
        if download_pdfs or True:
            # Zawsze pobieramy URLe załączników (ale PDF-y tylko z --download-pdfs)
            print(f"  Pobieram szczegóły artykułów...")
            enrich_with_attachments(
                session, records,
                download_pdfs=download_pdfs,
                pdf_dir=pdf_dir,
                debug=debug
            )

        all_records.extend(records)

    # Sortuj od najnowszych
    all_records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Statystyki
    interp = sum(1 for r in all_records if r["typ"] == "interpelacja")
    zap = sum(1 for r in all_records if r["typ"] == "zapytanie")
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Razem:        {len(all_records)}")

    # Usuń article_id z outputu (wewnętrzny)
    for r in all_records:
        r.pop("article_id", None)

    # Zapisz
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Sopot"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--kadencja", default="all",
        help="Kadencja do scrapowania: '2024-2029', '2018-2024' lub 'all' (domyślnie: all)"
    )
    parser.add_argument(
        "--download-pdfs", action="store_true",
        help="Pobierz pliki PDF interpelacji"
    )
    parser.add_argument(
        "--pdf-dir", default="pdfs/interpelacje",
        help="Katalog na PDF-y (domyślnie: pdfs/interpelacje)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache JSON dla /articles/{id} detail endpointów."
    )
    args = parser.parse_args()

    init_cache(args.cache_dir)
    if args.cache_dir:
        print(f"Cache JSON interpelacji: {args.cache_dir}")

    if args.kadencja == "all":
        kadencje = list(KADENCJE.keys())
    else:
        kadencje = [k.strip() for k in args.kadencja.split(",")]

    scrape(
        kadencje=kadencje,
        output_path=args.output,
        download_pdfs=args.download_pdfs,
        pdf_dir=args.pdf_dir if args.download_pdfs else None,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
