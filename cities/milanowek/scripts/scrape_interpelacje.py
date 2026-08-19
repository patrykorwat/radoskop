#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Milanówka.

Źródło: BIP Milanówka — sekcja "Interpelacje i zapytania".

    https://bip.milanowek.pl/interpelacje-i-zapytania.html

Po co: eSesja (https://milanowek.esesja.pl) NIE publikuje interpelacji — moduł
"Interpelacje i zapytania" jest nieaktywny ("Brak aktywności lub moduł
nieaktywny"). Rejestr prowadzony jest wyłącznie na BIP, w podkategoriach
"Interpelacje 2026" (interpelacje-2026.html) i "Interpelacje 2025"
(681-2025.html).

Struktura (CMS BIP z klasami `registry__*`):
  * Listing: tabele `<div class="registry__table_row" data-id="N">` z
    `<a class="registry__table_row_name registry__table_row_section">TYTUŁ</a>`,
    kolumną data dodania (`<time datetime="...">`) oraz kolumną publikacji.
    Link szczegółów jest RELATYWNY (np. `interpelacja-...html?`).
  * Szczegóły: TYLKO tytuł + załączniki PDF (class `file_add`):
      - treść: "Interpelacja..."/"Zapytanie..." (pierwszy załącznik)
      - odpowiedź: "Odpowiedź na interpelację"/"Odpowiedź na zapytanie"
    NIE MA w HTML imienia/nazwiska radnego ani daty złożenia — te są WYŁĄCZNIE
    wewnątrz skanowanych PDF-ów (SKM_C = skany). Tytuł podaje jedynie rodzaj
    ("radnej"/"radnego"/"radnych" — rodzaj gramatyczny, nie nazwisko).

Ograniczenia (uczciwie):
  * radny: puste — nazwisko tylko w skanie PDF (brak w HTML). NIE zgadujemy.
  * data_wplywu: puste — data złożenia tylko w skanie PDF (NIE bierzemy daty
    dodania na BIP jako daty złożenia — to by była nieścisłość).
  * data_odpowiedzi: puste — termin odpowiedzi w skanie PDF.
  * odpowiedz_status: "Udzielono" jeżeli na stronie jest PDF z odpowiedzią,
    w przeciwnym razie "Nie udzielono" (na razie brak pełnego tekstu odp.).
  * cri: używamy `data-id` rekordu z listing BIP (stabilny identyfikator strony),
    NIE jest to numer interpelacji z sesji (tego BIP nie podaje w HTML).
  * rok: z podkatalogu rejestru (2025 / 2026), w jakim opublikowano rekord.

Wynik: rekordy w formacie Radoskop (ten sam schemat co Przemyśl/Warszawa):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego brany z config.json (club_assignments -> clubs). Ponieważ radny
nie jest wykrywany w HTML, klub pozostanie pusty dopóki nie dodamy OCR PDF.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --max-pages 10
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

# Podkatalogi rejestru "Interpelacje i zapytania" na BIP Milanówka.
# Każdy to tabela registry__*. rok = rok podkatalogu (sposób organizacji BIP).
REGISTRIES = [
    ("2026", "https://bip.milanowek.pl/interpelacje-2026.html"),
    ("2025", "https://bip.milanowek.pl/681-2025.html"),
]

BASE = "https://bip.milanowek.pl/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MIN_ROK_DEFAULT = 2024  # bieżąca IX kadencja (2024-2029)

_DEBUG = False


def _load_clubs() -> tuple[dict, dict]:
    """(club_assignments, clubs) z config.json miasta."""
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    """Fetch z politeness delay + retry na przejściowe 403/5xx."""
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException:
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing listing
# ---------------------------------------------------------------------------


def parse_listing(html: str) -> list[dict]:
    """Zwraca listę {"cri","href","title","data_dodania"} z tabeli listing."""
    out = []
    if not html:
        return out
    starts = [m.start() for m in re.finditer(r'<div class="registry__table_row"[^>]*>', html)]
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(html)
        seg = html[s:e]
        did = re.search(r'data-id="(\d+)"', seg)
        a = re.search(r'<a href="([^"]+)"[^>]*class="registry__table_row_name[^"]*"[^>]*>(.*?)</a>', seg, re.S)
        if not a:
            continue
        title = re.sub(r"<[^>]+>", " ", a.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        dt = re.search(r'<time datetime="([^"]+)"', seg)
        out.append({
            "cri": did.group(1) if did else "",
            "href": a.group(1).strip(),
            "title": title,
            "data_dodania": dt.group(1) if dt else "",
        })
    return out


# ---------------------------------------------------------------------------
# Parsing detail
# ---------------------------------------------------------------------------

_FILE_RE = re.compile(r'<a[^>]*href="([^"]*file_add/download/[^"]+)"[^>]*>(.*?)</a>', re.S)


def parse_detail(html: str, bip_url: str, listing_year: str,
                 data_dodania: str) -> dict | None:
    if not html:
        return None

    title_m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = title_m.group(1).strip() if title_m else ""
    title = title.replace(" - BIP UM Milanówek", "").strip()

    files = [
        (re.sub(r"<[^>]+>", " ", label).strip(), href.strip())
        for href, label in _FILE_RE.findall(html)
    ]

    # typ z tytułu
    hay = title.lower()
    typ = "zapytanie" if "zapytanie" in hay else "interpelacja"

    tresc_url, odpowiedz_url = "", ""
    other = []
    for label, href in files:
        low = label.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = BASE + href
        else:
            other.append(BASE + href)
    # treść = pierwszy pozostały załącznik (interpelacja/zapytanie)
    if other:
        tresc_url = other[0]

    try:
        rok = int(listing_year)
    except (TypeError, ValueError):
        rok = 0

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    return {
        "cri": "",  # NIE znany z HTML (tytuł nie zawiera numeru)
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": "",  # nazwisko tylko w skanie PDF — NIE zgadujemy
        "przedmiot": title,
        "data_wplywu": "",  # data złożenia tylko w skanie PDF
        "klub": "",
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",  # termin odpowiedzi w skanie PDF
        "bip_url": bip_url,
        # -- pola pomocnicze (nie-schemat) zachowane dla audytu
        "_data_dodania": data_dodania,
        "_record_id": "",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Milanówka"
    )
    parser.add_argument("--output", default="docs/interpelacje.json",
                        help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true",
                        help="Szczegółowe logowanie")
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Limit liczby szczegółów do pobrania (0 = wszystkie; do testów)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Uwzględnij też starsze kadencje; domyślnie tylko IX (2024-2029)",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)

    # 1) pobierz listingi podkatalogów
    seen: dict[str, dict] = {}  # href -> meta (dedupe)
    for listing_year, url in REGISTRIES:
        _log(f"  listing {listing_year}: {url}")
        rows = parse_listing(fetch_text(_session(), url))
        for r in rows:
            key = r["href"]
            if key not in seen:
                seen[key] = {**r, "listing_year": listing_year}
        print(f"  {listing_year}: {len(rows)} rekordów na liście")

    print(f"  Listing: {len(seen)} unikalnych rekordów")
    if _DEBUG:
        for href, meta in sorted(seen.items()):
            print(f"    {meta['cri']}  {meta['title'][:60]}  [{meta['listing_year']}]")

    # 2) szczegóły
    records = []
    session = _session()
    count = 0
    for href, meta in sorted(seen.items()):
        if args.max_pages and count >= args.max_pages:
            break
        url = BASE + href.rstrip("?")
        html = fetch_text(session, url)
        rec = parse_detail(html, url, meta["listing_year"], meta["data_dodania"])
        if not rec:
            print(f"  [skip] brak treści: {url}")
            continue
        rec["_record_id"] = meta["cri"]
        rec["cri"] = meta["cri"]
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        count += 1
        if count % 10 == 0:
            print(f"  szczegóły: {count}...")

    # 3) sortowanie chronologiczne (data_dodania jako proxy, potem record_id)
    records.sort(key=lambda r: (r["_data_dodania"], r["_record_id"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


def _log(*a):
    if _DEBUG:
        print(*a)


if __name__ == "__main__":
    raise SystemExit(main())
