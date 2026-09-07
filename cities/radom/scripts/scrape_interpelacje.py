#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Radomiu.

Źródło: BIP Radomia — rejestr "Interpelacje i zapytania" pod zakładką
"Rada Miejska":

    https://bip.radom.pl/ra/rada-miejska/interpelacje-i-zapytani

Po co: Rada Miejska w Radomiu publikuje interpelacje/zapytania na BIP w
postaci strony rejestru z paginacją `?page=N` (BIP Radom korzysta z własnego
CMS-a o ścieżkach /ra/..., nie z eSesja/CCT).

Struktura:
  * Listing = strona rejestru, każdy rekord:
        <li class="list line"><a href="/ra/rada-miejska/interpelacje-i-zapytani/{ID},{SLUG}.html">
            <h3>Radny X - interpelacja w sprawie ...</h3>
            <div class="dataFloat">17 sierpnia 2026</div> ...
  * Szczegóły = strona z tytułem (h2: "Radny X - <typ> w sprawie <temat>"),
    sekcją "Pliki do pobrania" (załączniki PDF "Interpelacja" i "Odpowiedź")
    oraz "Metadane" (Data publikacji : DD.MM.RRRR).

Output: lista rekordów w formacie Radoskop (ten sam schemat co Przemyśl/
Warszawa/Bydgoszcz):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego brany jest z config.json (club_assignments -> clubs), tak samo
jak w pozostałych scraperach.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --max-pages 5   # test
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all            # bez filtra roku
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

from http_cache import init_cache, cached_fetch_text  # noqa: E402

# TTL cache dla stron szczegółowych (stabilne URL-e). Listingi zawsze force.
DETAIL_TTL = 3 * 86400

REJESTR_URL = "https://bip.radom.pl/ra/rada-miejska/interpelacje-i-zapytani"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
# Rejestr Radomia liczy ~219 stron (stan sierpień 2026). Górny limit bezpieczeństwa.
MAX_PAGES = 250
# Domyślnie tylko bieżąca kadencja (IX, 2024-2029). Starsze dostępne przez --all.
MIN_ROK_DEFAULT = 2024

_DEBUG = False

# --- polskie nazwy miesięcy: "17 sierpnia 2026" -> 2026-08-17 ---
_PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}


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


def fetch_text(session: requests.Session, url: str, *, force: bool = False,
               ttl: float | None = DETAIL_TTL) -> str:
    """Fetch z disk cache (TTL) + politeness delay + retry na 403/429/5xx."""
    for attempt in range(3):
        try:
            return cached_fetch_text(url, session=session, timeout=30,
                                     delay=DELAY, force=force, ttl=ttl)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def fetch_listing(session: requests.Session, page: int) -> str:
    url = REJESTR_URL if page == 1 else f"{REJESTR_URL}?page={page}"
    # Listing: zawsze HTTP — nowe wpisy przesuwa stronicowanie.
    return fetch_text(session, url, force=True, ttl=0)


# ---------------------------------------------------------------------------
# Parsing — listing
# ---------------------------------------------------------------------------

# Blok pojedynczego rekordu na liście: link do szczegółów + h3 (tytuł) + dataFloat.
_LIST_ITEM_RE = re.compile(
    r'<li class="list line">.*?'
    r'href="(/ra/rada-miejska/interpelacje-i-zapytani/(\d+),[^"]*\.html)"'
    r'.*?<h3>(.*?)</h3>.*?'
    r'<div class="dataFloat">(.*?)</div>',
    re.S,
)


def parse_listing(html: str) -> list[dict]:
    """Zwraca listę {bip_url, cri, title, date_float} unikalnych po bip_url."""
    if not html:
        return []
    out: dict[str, dict] = {}
    for m in _LIST_ITEM_RE.finditer(html):
        href, doc_id, title, datef = m.group(1), m.group(2), m.group(3), m.group(4)
        url = "https://bip.radom.pl" + href
        out[url] = {
            "bip_url": url,
            "cri": doc_id.strip(),
            "title": re.sub(r"\s+", " ", title).strip(),
            "date_float": re.sub(r"\s+", " ", datef).strip(),
        }
    return list(out.values())


# ---------------------------------------------------------------------------
# Parsing — szczegóły
# ---------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_pl_date(text: str) -> str:
    """'17 sierpnia 2026' -> '2026-08-17' (lub '' jeśli nie da się sparsować)."""
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", text or "", re.I)
    if not m:
        return ""
    d, mon, y = m.groups()
    if mon.lower() not in _PL_MONTHS:
        return ""
    return f"{y}-{_PL_MONTHS[mon.lower()]:02d}-{int(d):02d}"


def normalize_date_dmy(dd_mm_yyyy: str) -> str:
    """'21.07.2026' -> '2026-07-21' (format z metadanych BIP)."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_FILES_RE = re.compile(
    r'<li>\s*<a href="(/download/[^"]+)"[^>]*>'
    r'\s*<strong>(.*?)</strong>\s*'
    r'<span[^>]*>(.*?)</span>',
    re.S,
)
_META_DATE_RE = re.compile(r"Data publikacji\s*:\s*([\d.]+)")


def parse_title(html: str) -> str:
    """Tytuł z <h2> (główny tytuł treści szczegółów)."""
    for m in _TITLE_RE.finditer(html):
        t = _strip_tags(m.group(1))
        if "interpelacj" in t.lower() or "zapytan" in t.lower():
            return t
    return ""


def parse_detail(html: str, listing: dict) -> dict | None:
    """Zbuduj rekord z treści strony szczegółów + dane z listingu."""
    if not html:
        return None

    title = parse_title(html) or listing.get("title", "")
    if not title:
        return None

    # --- radny, typ, przedmiot z tytułu ---
    # Wzorce: "Radny X - interpelacja/zapytanie w sprawie Y"
    #         "Radni A, B i C - interpelacja w sprawie Y"
    low = title
    if "zapytanie" in low.lower() and "interpelacja" not in low.lower():
        typ = "zapytanie"
    else:
        typ = "interpelacja"

    # Autor: wszystko przed pierwszym " - ". Obsługuje "Radny X", "Radna X",
    # "Radni A, B i C" oraz autorów zbiorowych typu "Klub Radnych Prawa i
    # Sprawiedliwości" (niektóre interpelacje składane są przez klub).
    dash = low.find(" - ")
    author_raw = low[:dash].strip() if dash != -1 else low
    # Usuń prefiks rodzajowy ("Radny"/"Radna"/"Radni"/"Radne").
    author_raw = re.sub(r"^\s*(Radn[yi]|Radna|Radne)\s+", "", author_raw)
    # Przy współautorach bierzemy pierwsze imię+nazwisko (spójne z resztą Radoskop).
    parts = [p.strip() for p in re.split(r"[,\n]+", author_raw) if p.strip()]
    radny = parts[0] if parts else author_raw
    # "i Mateusz Kuźmiuk" po pierwszym autorze — odcinamy " i ..." przy jednym autorze
    radny = re.sub(r"\s+i\s+[A-ZĄĆĘŁŃÓŚŹŻ].*$", "", radny).strip()

    # Przedmiot: tekst po " w sprawie " (jeśli występuje), inaczej reszta po " - ".
    przedmiot = ""
    if " w sprawie " in low:
        przedmiot = low.split(" w sprawie ", 1)[1].strip()
    else:
        dash = low.find(" - ")
        przedmiot = low[dash + 3:].strip() if dash != -1 else low

    # --- data wpływu: metadane (Data publikacji) > dataFloat z listingu ---
    md = _META_DATE_RE.search(html)
    data_wplywu = normalize_date_dmy(md.group(1)) if md else ""
    if not data_wplywu:
        data_wplywu = parse_pl_date(listing.get("date_float", ""))

    # --- załączniki: "Interpelacja"/"Odpowiedź" ---
    tresc_url = ""
    odpowiedz_url = ""
    data_odpowiedzi = ""
    for href, label, meta in _FILES_RE.findall(html):
        lab = _strip_tags(label).lower()
        full = "https://bip.radom.pl" + href
        # data pliku w meta np. "pdf, 3.32 MB, 28.07.2026"
        fdate = ""
        dm = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", meta)
        if dm:
            fdate = normalize_date_dmy(dm.group(1))
        if "odpowied" in lab:
            if not odpowiedz_url:
                odpowiedz_url = full
                if fdate:
                    data_odpowiedzi = fdate
        elif "interpelacj" in lab or "zapytan" in lab:
            if not tresc_url:
                tresc_url = full
        elif not tresc_url and "pdf" in lab:
            tresc_url = full

    rok = 0
    if data_wplywu:
        try:
            rok = int(data_wplywu[:4])
        except (ValueError, TypeError):
            rok = 0

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    return {
        "cri": listing.get("cri", ""),
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if (data_odpowiedzi or odpowiedz_url) else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": listing.get("bip_url", ""),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG, MIN_ROK_DEFAULT
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Radomia"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj wszystkie lata; domyślnie tylko IX kadencja (rok>=2024)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=MAX_PAGES,
        help="Limit stron listingu (głównie do testów)",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT
    max_pages = args.max_pages

    init_cache(args.cache_dir)

    session = _session()

    print("=== Interpelacje — BIP Radomia ===")
    listing_by_url: dict[str, dict] = {}
    empty_streak = 0
    page = 1
    while page <= max_pages:
        html = fetch_listing(session, page)
        items = parse_listing(html)
        new_items = [it for it in items if it["bip_url"] not in listing_by_url]
        _log(f"  strona {page}: {len(items)} pozycji, nowych: {len(new_items)}")
        if not items:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        for it in new_items:
            listing_by_url[it["bip_url"]] = it
        if page % 20 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(listing_by_url)} pozycji)")
        page += 1
    print(f"  Listing: {len(listing_by_url)} interpelacji/zapytań (strony 1..{page - 1})")

    records = []
    fetched = 0
    for i, (url, listing) in enumerate(listing_by_url.items(), start=1):
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, listing)
        if not rec:
            continue
        fetched += 1
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

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
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
