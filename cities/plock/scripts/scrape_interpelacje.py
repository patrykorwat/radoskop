#!/usr/bin/env python3
"""Scraper interpelacji radnych Rady Miasta Płocka.

Źródło: BIP Płock — nowybip.plock.eu (platforma nowy BIP, podstrona
„Interpelacje Radnych Miasta"). Uwaga: domena `nowybip.plock.eu`
jest prawdziwym BIP-em (bip_url z config.json `https://nowybip.plock.eu/`).

Struktura listingu (per rok, paginowany `?page=N`):
  /interpelacje/{token}  — roczne rejestry (title „Interpelacje z RRRR roku")
  element listy:
    <a href="https://nowybip.plock.eu/interpelacja/{ID}" class="normal">
       Interpelacja: {RADNY}, Interpelacja Nr {NR} z dnia {D} {MIESIĄC} {R} r.</a>
    <BR>{PRZEDMIOT} <HR>

  Przedmiot i radny (mianownik) + nr + data są jawnie w liście — nie trzeba
  parsować skanów PDF.

Struktura detalu (/interpelacja/{ID}):
  * Przedmiot jako nagłówek treści (HTML inline, bez PDF interpelacji)
  * Załącznik „Odpowiedź": /dokumenty/nowybip/dok/{rok}/{ID}/{hash}.pdf
    -> odpowiedz_status=Udzielono, odpowiedz_url=PDF
  * treść interpelacji jest na stronie (nie w osobnym PDF) — tresc_url=bip_url

Tylko interpelacje (Płock nie publikuje osobnego rejestru zapytań; /zapytania
renderuje tą samą stronę główną).

Radny dopasowywany do config.json club_assignments (fuzzy diacritic, próg 0.72).
Autorzy zbiorowi (Klub/Komisja) zostają z klub="".

Output: format Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url,
data_odpowiedzi, bip_url}.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
"""

import argparse
import difflib
import html as htmllib
import json
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache, cached_fetch_text  # noqa: E402

BIP_BASE = "https://nowybip.plock.eu"
# PEŁNY rejestr "Interpelacje Radnych" (lista /interpelacje, paginowana ?page=N).
# UWAGA: rokowe podstrony "Interpelacje z RRRR roku" (/interpelacje/{token})
# zawierają tylko PODZBIÓR (widget) — pełny rejestr jest na /interpelacje
# (reverse-chron: strona 1 = najnowsze). Dlatego scraper crawla pełną listę
# i filtruje rok>=2024 (IX kadencja). Rokowe tokeny zostają tylko w komentarzu.
#   2024 token hIvOqbT3, 2025 token hBkmlxQx, 2026 token Y99xejvm (niepełne)
LISTING_BASE = "https://nowybip.plock.eu/interpelacje"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.7
MAX_PAGES = 200
PER_PAGE = 25
_DEBUG = False

# polskie miesiące -> numer
_MONTHS = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
}

_ITEM_RE = re.compile(
    r'<a\s+href="(https://nowybip\.plock\.eu/interpelacja/([A-Za-z0-9]+))"[^>]*>'
    r'\s*Interpelacja:\s*([^,]+?),\s*Interpelacja\s*Nr\s*(\d+)\s*z\s*dnia\s*'
    r'(\d{1,2})\s+([a-ząęóśłżźćń]+)\s+(\d{4})'
    r'[^<]*</a>\s*<BR>\s*(.*?)</li>', re.S)


def _load_clubs() -> tuple[dict, dict]:
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


def _fuzzy_club_radny(name: str) -> str:
    if name in _CLUB_ASSIGN:
        return name
    best_key, best_score = "", 0.0
    for key in _CLUB_ASSIGN:
        s = difflib.SequenceMatcher(None, name.lower(), key.lower()).ratio()
        if s > best_score:
            best_score, best_key = s, key
    return best_key if best_score >= 0.72 else name


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _pl_date(d: str, mon: str, y: str) -> str:
    mo = _MONTHS.get(mon.lower())
    if not mo:
        return ""
    return f"{y}-{mo}-{int(d):02d}"


def parse_listing(html: str) -> list[dict]:
    """-> [{"bip_url","id","radny_raw","nr","date","przedmiot"}]"""
    out = []
    for m in _ITEM_RE.finditer(html):
        href, rid, radny, nr, d, mon, y, subject = m.groups()
        date = _pl_date(d, mon, y)
        out.append({
            "bip_url": href,
            "id": rid,
            "radny_raw": htmllib.unescape(radny).strip(),
            "nr": int(nr),
            "date": date,
            "przedmiot": htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", subject))).strip(),
        })
    return out


def parse_detail(html: str, item: dict, rok: int) -> dict:
    radny = _fuzzy_club_radny(item["radny_raw"])
    odp = ""
    m = re.search(r'href="(/dokumenty/[^"]*dok/[^"]+\.pdf)"', html)
    if m:
        odp = m.group(1)
    odp_url = (BIP_BASE + odp) if odp else ""
    rok2 = rok or (int(item["date"][:4]) if item["date"] else 0)
    return {
        "cri": f"{item['nr']}/{rok2}" if rok2 else f"{item['nr']}",
        "typ": "interpelacja",
        "rok": rok2,
        "kadencja": "2024-2029" if rok2 >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": item["przedmiot"],
        "data_wplywu": item["date"],
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odp_url else "Nie udzielono",
        "tresc_url": item["bip_url"],
        "odpowiedz_url": odp_url,
        "data_odpowiedzi": "",
        "bip_url": item["bip_url"],
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji z BIP Płocka")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też starsze kadencje; domyślnie tylko 2024+")
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    years_used = None
    min_rok = None if args.all else 2024

    print("=== Interpelacje — BIP Płock ===")
    seen: dict[str, dict] = {}
    page = 1
    empty_streak = 0
    while page <= args.max_pages:
        url = f"{LISTING_BASE}?page={page}" if page > 1 else LISTING_BASE
        html = cached_fetch_text(url, session=session, headers=HEADERS,
                                 timeout=30, delay=DELAY)
        rows = parse_listing(html)
        new = [r for r in rows if r["bip_url"] not in seen]
        if not new:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0
        for r in new:
            seen[r["bip_url"]] = r
        # reverse-chron lista: gdy wszystkie rekordy na stronie są starsze niż min_rok, stop
        yrs = [int(r["date"][:4]) for r in rows if r["date"]]
        if min_rok and yrs and max(yrs) < min_rok and page > 2:
            print(f"  stop na stronie {page} (wszystkie < {min_rok})")
            break
        if page % 10 == 0 and not _DEBUG:
            print(f"  strona {page}... ({len(seen)} znalezionych)")
        page += 1
    if not args.all:
        years_used = [y for y in range(2024, 2027)]

    print(f"  Listing: {len(seen)} unikalnych rekordów")

    records = []
    for item in seen.values():
        html = cached_fetch_text(item["bip_url"], session=session, headers=HEADERS,
                                 timeout=30, delay=DELAY)
        if not html:
            print(f"  [skip] brak treści: {item['bip_url']}")
            continue
        year = int(item["date"][:4]) if item["date"] else 0
        rec = parse_detail(html, item, year)
        if not rec["rok"] or (not args.all and rec["rok"] < 2024):
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
