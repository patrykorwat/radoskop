#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Marki.

Źródło: BIP Marki — moduł "Interpelacje i zapytania radnych od 2026 r".

    https://bip.marki.pl/interpelacje/1919

Od sierpnia 2026 ten moduł jest AKTYWNY (w przeciwieństwie do eSesja
marki.esesja.pl, który zwraca "Brak aktywności lub moduł nieaktywny") i stanowi
jedyny czysty, parsowalny rejestr interpelacji/zapytań miasta. Rejestr
starszych wpisów (do grudnia 2025) miasto publikuje jako JEDEN duży artykuł
(https://bip.marki.pl/artykuly/1837/interpelacje-i-zapytania-radnych-do-grudnia-2025-r)
bez struktury rejestru (brak Nr sprawy/szczegółów) — NIE jest to czysto
parsowalne źródło (uczciwa luka / partial).

Struktura:
  * Listing = tabela składowa z paginacją `?page=N` (10 rekordów/stronę).
    Każdy rekord to `<table class="table table-borderless">` z caption
    "Interpelacja w sprawie : <przedmiot>" (lub "Zapytanie w sprawie ...")
    oraz wierszami th/td: "Nr sprawy" (cri), "Tożsamość radnego" (radny),
    "Interpelacja/Zapytanie w sprawie" -> <a href="/interpelacja/{id}/..."> (detail).
  * Szczegóły (detail) = strona /interpelacja/{id}/... z polami:
        Typ wystąpienia, Nr sprawy, Tożsamość radnego, w sprawie,
        "Data przekazania: DD.MM.YYYYr."  (data_wplywu)
      oraz załącznikami:
        <a href="https://bip.marki.pl/attachments/download/{id}">(tresc / Odpowiedź)
      "Odpowiedź Burmistrza Miasta Marki" + pdf z odpowiedzią => odpowiedz_status
      "Udzielono", w przeciwnym razie "Nie udzielono". BIP nie publikuje jawnej
      daty odpowiedzi -> data_odpowiedzi zostaje puste (honest).

Output: lista rekordów w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego jest brany z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --max-pages 3
"""

import argparse
import difflib
import json
import re
import sys
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

REJESTR_URL = "https://bip.marki.pl/interpelacje/1919"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MAX_PAGES = 30
MIN_ROK_DEFAULT = 2024

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
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
    if not radny:
        return ""
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        # fuzzy diacritic fallback (metryczka bywa ASCII/literówką vs klucze config)
        best, bs = "", 0.0
        for key in _CLUB_ASSIGN:
            s = difflib.SequenceMatcher(None, radny.lower(), key.lower()).ratio()
            if s > bs:
                best, bs = key, s
        if bs >= 0.72:
            code = _CLUB_ASSIGN.get(best, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30, verify=False)
            if resp.status_code == 200 and len(resp.text) > 1000:
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r'<table class="table table-borderless">(.*?)</table>', re.S)
_CAPTION_RE = re.compile(r"<caption[^>]*>(.*?)</caption>", re.S)
_ROW_RE = re.compile(r'<th scope="row">(.*?)</th>\s*<td[^>]*>(.*?)</td>', re.S)
_DETAIL_LINK_RE = re.compile(r'href="(https://bip\.marki\.pl/interpelacja/\d+[^"]*)"')


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_listing(html: str) -> list[dict]:
    """Zwraca listę rekordów z listingu (bez data_wplywu i załączników)."""
    out = []
    for table in _TABLE_RE.findall(html):
        cap_m = _CAPTION_RE.search(table)
        cap = _clean(cap_m.group(1)) if cap_m else ""
        rows = {_clean(k): _clean(v) for k, v in _ROW_RE.findall(table)}
        link_m = _DETAIL_LINK_RE.search(table)
        bip_url = link_m.group(1) if link_m else ""
        cri = rows.get("Nr sprawy", "")
        radny = rows.get("Tożsamość radnego", "")
        # typ + przedmiot z caption: "Interpelacja w sprawie : <x>" / "Zapytanie w sprawie : <x>"
        typ = "zapytanie" if cap.lower().startswith("zapytanie") else "interpelacja"
        przedmiot = _clean(re.sub(r"^(?:Interpelacja|Zapytanie)\s+w sprawie\s*:?\s*", "", cap))
        if not bip_url and not cri:
            continue
        out.append({
            "cri": cri, "typ": typ, "radny": radny,
            "przedmiot": przedmiot or rows.get("w sprawie", ""),
            "bip_url": bip_url,
        })
    return out


# caption bez tagów
_CLEAN_CAP = re.compile(r"<[^>]+>")


_DATAPRZEK_RE = re.compile(r"Data przekazania:\s*([\d.]+)")
_ATTACH_RE = re.compile(
    r'<a[^>]*href="(https://bip\.marki\.pl/attachments/download/\d+)"[^>]*>(.*?)</a>',
    re.S,
)
_TYP_RE = re.compile(r"Typ wystąpienia</th>\s*<td[^>]*>(.*?)</td>", re.S)
_NR_RE = re.compile(r"Nr sprawy</th>\s*<td[^>]*>(.*?)</td>", re.S)
_RADNY_RE = re.compile(r"Tożsamość radnego</th>\s*<td[^>]*>(.*?)</td>", re.S)


def parse_detail(html: str, listing: dict) -> dict:
    if not html:
        return None
    typ_m = re.search(r"Typ wystąpienia\s*</th>\s*<td[^>]*>(.*?)</td>", html, re.S)
    typ = "interpelacja"
    if typ_m:
        t = _clean(typ_m.group(1)).lower()
        if "zapytanie" in t:
            typ = "zapytanie"
        elif "interpelacja" in t:
            typ = "interpelacja"
    rok = 0
    m = re.search(r"(?:BRM\.\d+\.\d+\.|/interpelacja/\d+/)(\d{4})", html) or re.search(r"(\d{4})", html)
    if m:
        try:
            rok = int(m.group(1))
        except ValueError:
            rok = 0

    # data_wplywu z "Data przekazania: DD.MM.YYYYr."
    data_wplywu = ""
    dm = _DATAPRZEK_RE.search(html)
    if dm:
        data_wplywu = normalize_date(dm.group(1))

    # załączniki: tresc = non-odpowiedź; odpowiedz = plik z "odpowiedź"
    tresc_url = ""
    odpowiedz_url = ""
    answered = False
    for href, label in _ATTACH_RE.findall(html):
        low = _clean(label).lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
                answered = True
        else:
            if not tresc_url:
                tresc_url = href
    # jeżeli jest tylko odpowiedź a nie treść — traktuj odpowiedź też jako tresc? nie: zostaw
    odpowiedz_status = "Udzielono" if answered else "Nie udzielono"

    radny = listing.get("radny", "")
    # współautorzy: bierzemy pierwszego radnego (jak w kanonicznym scraperze) do klubu
    parts = [p.strip() for p in re.split(r"[,\n]+", radny) if p.strip()]
    radny_first = parts[0] if parts else radny
    cri = listing.get("cri", "")
    przedmiot = listing.get("przedmiot", "")

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024" if rok >= 2018 else ""

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny_first,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny_first),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": listing.get("bip_url", ""),
    }


def normalize_date(dd_mm_yyyy: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG, MAX_PAGES
    parser = argparse.ArgumentParser(description="Scraper interpelacji z BIP Marki")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="Limit stron listingu (testy)")
    parser.add_argument("--all", action="store_true",
                        help="Bez filtra roku (domyślnie min_rok=2024)")
    args = parser.parse_args()
    _DEBUG = args.debug
    MAX_PAGES = args.max_pages
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print(f"=== Interpelacje — BIP Marki ({REJESTR_URL}) ===")
    listings = {}  # bip_url -> listing record
    page = 1
    empty_streak = 0
    while page <= MAX_PAGES:
        url = f"{REJESTR_URL}?page={page}"
        time.sleep(DELAY)
        html = fetch_text(session, url)
        recs = parse_listing(html)
        _log(f"  strona {page}: {len(recs)} rekordów")
        new = 0
        for r in recs:
            if r["bip_url"] and r["bip_url"] not in listings:
                listings[r["bip_url"]] = r
                new += 1
        if new == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        if page % 5 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(listings)} znalezionych)")
        page += 1
    print(f"  Listing: {len(listings)} rekordów w rejestrze")

    records = []
    fetched = 0
    for bip_url, rec in listings.items():
        html = fetch_text(session, bip_url)
        if not html:
            print(f"  [skip] brak treści: {bip_url}")
            continue
        detail = parse_detail(html, rec)
        if not detail:
            continue
        fetched += 1
        if min_rok and detail["rok"] and detail["rok"] < min_rok:
            continue
        records.append(detail)
        if fetched % 20 == 0:
            print(f"  szczegóły: {fetched}...")

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
