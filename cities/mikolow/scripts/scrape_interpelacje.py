#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej Mikołowa.

Źródło: BIP Mikołowa (backend: Next.js React-SPA, dane w danych lotu
`self.__next_f.push`). Rejestr jest realny i statyczny — każda interpelacja/
zapytanie ma osobną stronę artykułu:

    https://bip.mikolow.eu/kategorie/369-kadencja-20242029/artykuly/{id}-{slug}
    (kategoria 369 = "Kadencja 2024-2029" pod "Interpelacje i zapytania radnych")

UWAGA na adres: domena BIP Mikołowa to **bip.mikolow.eu** (bip.mikolow.pl to
główna strona miasta/mikolow.eu, NIE jest to BIP). Konfiguracyjny bip_url
`https://bip.mikolow.pl/` w config.json jest błędny i pokazuje stronę miasta.

Struktura strony szczegółów (Next.js RSC flight data):
  * id            -> "cri"
  * publishedDate -> "data_wplywu" (data publikacji na BIP — najbliższa
    dostępna strukturalnie data; faktyczna data złożenia bywa w środku
    skanu-PDF treści, bez warstwy tekstowej, więc jej NIE wyciągamy).
  * title         -> radny / typ / przedmiot (np. "Radna Katarzyna Głośna -
                     interpelacja w sprawie ...", "Klub radnych ... - ...").
  * attachments (PDF) -> "tresc_url" (treść) i "odpowiedz_url" (odpowiedź);
    typ rozpoznawany po słowie "odpowiedź" w nazwie pliku. Odpowiedź-status =
    "Udzielono" iff plik odpowiedzi istnieje.

Klub radnego brany z config.json (club_assignments -> clubs), tak samo jak
w scrape_przemysl.py. Interpelacje składane przez "Klub radnych ..." nie mają
pojedynczego radnego -> radny="" (klub pozostaje z nazwy zdeponowanej w tytule
a pole klub zostaje puste, bo nie ma mapowania), uczciwie bez zmyślania.

Listing: kategoria 369 paginowana `?page=N` (10 pozycji/stronę, ~7 stron dla 66
wpisów). Starannie filtrujemy wyłącznie artykuły typu interpelacja
(`-interpelacja-`/`-zapytanie-` w slugu), pomijając mechaniczne wpisy listingu.

Output: format Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --max-pages 2
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także VIII kadencja (kat. 89, 2018-2024)
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

# Kategorie "Interpelacje i zapytania radnych" na BIP Mikołowa:
#   * 369 = IX kadencja "Kadencja 2024-2029" (domyślnie)
#   * 89  = VIII kadencja "Kadencja 2018-2024 (od 1.01.2023)" (przez --all)
CATEGORIES = {
    "ix": {"slug": "kategorie/369-kadencja-20242029", "kadencja": "2024-2029", "min_rok": 2024},
    "viii": {"slug": "kategorie/89-kadencja-20182024-od-1012023", "kadencja": "2018-2024", "min_rok": 2023},
}
BASE = "https://bip.mikolow.eu"
DEFAULT_SLUG = CATEGORIES["ix"]["slug"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MAX_PAGES = 120
# Domyślnie tylko bieżąca (IX) kadencja: rok >= 2024.
MIN_ROK_DEFAULT = 2024

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


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
_RADNI = sorted(_CLUB_ASSIGN.keys(), key=len, reverse=True)  # dłuższe najpierw (Syryjczyk-Słomska)


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    """Fetch z politeness delay + retry na przejściowych 403/5xx/4xx."""
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and "<!doctype html".encode() in resp.text.encode().lower():
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429, 500, 502, 503):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Next.js RSC flight-data extraction
# ---------------------------------------------------------------------------

_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[\d+,"((?:[^"\\]|\\.)*)"\]\)')


def flight_buffer(html: str) -> str:
    """Wypakowuje dane lotu Next.js RSC do jednego bufora tekstu."""
    if not html:
        return ""
    out = []
    for m in _FLIGHT_RE.finditer(html):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            continue
    return "\n".join(out)


# --- listing ---
_SLUG_RE = re.compile(r'"slug":"((?:kategorie/)?\d+-kadencja-[^"]*/artykuly/\d+[^"]*)"')


def parse_listing(buf: str, category_slug: str) -> list[str]:
    """Zwraca listę absolutnych URL artykułów z bufora listingu kategorii.

    Kategoria jest rejestrem interpelacji/zapytań — bierzemy wszystkie jej
    artykuły (slug zawiera `<category_slug>/artykuly/`), bez nadmiernego
    filtrowania słownego (klubowe tytuły bywają "Interpelacja Klubu ...").
    """
    marker = category_slug + "/artykuly/"
    out = []
    for slug in dict.fromkeys(_SLUG_RE.findall(buf)):
        if marker not in slug:
            continue
        if not slug.startswith("kategorie/"):
            slug = "kategorie/" + slug
        u = f"{BASE}/{slug}"
        u = u.split("?")[0]
        if u not in out:
            out.append(u)
    return out


# --- detail ---
_ATT_RE = re.compile(
    r'"url":"(https://bip-api\.mikolow\.eu/api/attachments/\d+)",'
    r'"size":"[^"]*","content":"","contentType":"application/pdf",'
    r'"filename":"([^"]+)"'
)
_ID_PUB_RE = re.compile(r'"id":(\d+),"publishedDate":"([\d\-T:\.Z]+)"')
# artykuł: id + publishedDate + title (klubowe tytuły zaczynają się od
# "Interpelacja Klubu ...", indywidualne od "Radna/Radny <Nazwisko> - ...").
_ART_RE = re.compile(r'"id":\d+,"publishedDate":"[^"]+","title":"((?:[^"\\]|\\.)*)"')


def parse_detail(html: str, bip_url: str) -> dict | None:
    if not html:
        return None
    buf = flight_buffer(html)
    if not buf:
        return None

    m = _ID_PUB_RE.search(buf)
    ma = _ART_RE.search(buf)
    if not m or not ma:
        return None
    cri, pub = m.groups()
    title = ma.group(1).replace("\\", "").strip()
    # tytuł musi dotyczyć interpelacji/zapytania (bufor zawiera też tytuły menu)
    if not re.search(r"interpelacj|zapytan", title, re.I):
        return None

    rok = int(pub[:4])
    data_wplywu = pub[:10]  # data publikacji na BIP (proxy dla daty wpływu)

    # radny / przedmiot / typ z tytułu
    radny = ""
    for name in _RADNI:
        if name in title:
            radny = name
            break
    typ = "zapytanie" if re.search(r"\bzapytani", title, re.I) else "interpelacja"
    # przedmiot = tytuł bez prefiksu autora
    przedmiot = re.sub(r"^(?:Radn[ya]|Radny|Klub radnych)[^\-]*-\s*", "", title).strip()
    if not przedmiot:
        przedmiot = title

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    # załączniki: podział treść / odpowiedź po nazwie pliku
    tresc_url, odpowiedz_url = "", ""
    for url, fname in _ATT_RE.findall(buf):
        if re.search(r"odpowiedz|odpowiedź", fname, re.I):
            if not odpowiedz_url:
                odpowiedz_url = url
        else:
            if not tresc_url:
                tresc_url = url

    data_odpowiedzi = date_from_filename(odpowiedz_url, buf)
    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": bip_url,
    }


def date_from_filename(odpowiedz_url: str, buf: str) -> str:
    """Data odpowiedzi z nazwy pliku odpowiedzi (np. 'z dnia 28.05.24 r.')
    lub z pola liczby/etykiety odpowiedzi; brak -> ''.
    """
    if not odpowiedz_url:
        return ""
    att_id = odpowiedz_url.rstrip("/").rsplit("/", 1)[-1]
    # znajdź filename powiązany z tym attachment id
    fm = re.search(
        r'"url":"' + re.escape(odpowiedz_url) + r'"[^}]{0,200}?"filename":"([^"]+)"',
        buf,
    )
    fname = fm.group(1) if fm else ""
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{2,4})", fname or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(mo):02d}-{int(d):02d}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Mikołowa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="Maks. stron listingu")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też VIII kadencję (kat. 89); domyślnie tylko IX (2024-2029)",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT
    max_pages = args.max_pages

    init_cache(args.cache_dir)
    session = _session()

    slugs = [CATEGORIES["ix"]["slug"]]
    if args.all:
        slugs.append(CATEGORIES["viii"]["slug"])

    seen: dict[str, str] = {}  # artykuł URL -> detail html
    for cat_slug in slugs:
        print(f"=== Interpelacje — BIP Mikołowa ({cat_slug}) ===")
        empty = 0
        page = 1
        while page <= max_pages:
            url = f"{BASE}/{cat_slug}?page={page}&lang=PL"
            time.sleep(DELAY)
            html = fetch_text(session, url)
            links = parse_listing(flight_buffer(html), cat_slug) if html else []
            new = [u for u in links if u not in seen]
            _log(f"  strona {page}: {len(links)} linków, nowych: {len(new)}")
            if not new:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
            for u in new:
                seen[u] = ""
            if page % 10 == 0 and not _DEBUG:
                print(f"  listing strona {page}... ({len(seen)} znalezionych)")
            page += 1
        print(f"  {cat_slug}: do strony {page - 1}")

    print(f"\nListing: {len(seen)} artykułów interpelacji/zapytań")

    records = []
    fetched = 0
    for i, url in enumerate(seen, start=1):
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            print(f"  [skip] nie sparsowano: {url}")
            continue
        fetched += 1
        if min_rok is not None and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if fetched % 25 == 0:
            print(f"  szczegóły: {fetched}...")

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    novlos = [r["cri"] for r in records if not r["radny"]]
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")
    if novlos:
        print(f"Bez radnego (klub/puste): {len(novlos)}  {novlos[:12]}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
