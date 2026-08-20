#!/usr/bin/env python3
"""Scraper interpelacji, zapytań i wniosków radnych Rady Miejskiej w Ostródzie.

Źródło: BIP Ostródy (CMS Logonet, domena bipostroda.warmia.mazury.pl) — kategoria
"Wnioski i interpelacje Radnych 2024-2029" (id 1010).

Struktura:
  * Listing = kategoria z tabelą (c-grid-table), rekordy <tr data-key="ID">:
    - <td data-col-date>{DD-MM-YYYY}</td>  (data wpływu)
    - <td data-col-main><a href="/{id}/{slug}.html">{tytuł}</a></td>
    Paginacja przez ?page=N. Domyślnie 10 str./rekordów (ostatnia strona).
  * Tytuł koduje typ (Interpelacja/Zapytanie/Wniosek) + autora + przedmiot
    ("...w sprawie..."). Autor w dopełniaczu (np. "Beaty Horodyłowskiej Radnej").
  * Detal: załączniki (post-attach-link .bi-file-pdf) do /attachment/informacja/{id}/... 
    — treść oraz "Odpowiedź na ..."; metryka "Osoba, która odpowiada za treść"
    daje autora w mianowniku.

Output: rekordy w schemacie Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}.

Radny dopasowywany do config.json club_assignments (fuzzy diacritic). Radnych
spoza config (np. Horodyłowska) zostawiamy z klub="" — nie zgadujemy klubów.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
"""

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

LISTING_URL = (
    "https://bipostroda.warmia.mazury.pl/kategoria/1010/"
    "wnioski-i-interpelacje-radnych-2024-2029.html"
)
BIP_BASE = "https://bipostroda.warmia.mazury.pl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.55
MAX_PAGES = 20

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


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
    """Kanoniczny radny z club_assignments przez fuzzy diacritic dopasowanie."""
    if name in _CLUB_ASSIGN:
        return name
    # BIP Ostródy podaje autorów "Last First" (metryka) — spróbuj też odwrotnie.
    toks = name.split()
    candidates = [name]
    if len(toks) == 2:
        candidates.append(" ".join(reversed(toks)))
    best_key, best_score = "", 0.0
    for cand in candidates:
        for key in _CLUB_ASSIGN:
            s = difflib.SequenceMatcher(None, cand.lower(), key.lower()).ratio()
            if s > best_score:
                best_score, best_key = s, key
    if best_score >= 0.72:
        return best_key
    return name


def _canonical_radny(raw: str) -> str:
    """Mianownik autora (trim "Radna/Radny Rady Miejskiej w Ostródzie")."""
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    name = re.sub(
        r"\s*(Rady Miejskiej\s+)?w\s+Ostr[oó]dzie.*$", "", name, flags=re.I
    ).strip()
    name = re.sub(
        r"\s*(Radna|Radny|Radn[ae]|Radnych)\b.*$", "", name, flags=re.I
    ).strip()
    name = name.strip(" ,;:-") 
    if name and "," in name:
        name = name.split(",")[0].strip()
    if not name:
        return ""
    return _fuzzy_club_radny(name)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url: str) -> str:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def fetch_listing(session, page: int) -> str:
    url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
    time.sleep(DELAY)
    return fetch_text(session, url)


def normalize_date(dd_mm_yyyy: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_listing(html: str) -> list[dict]:
    """-> [{"id", "date", "href", "title"}]"""
    out = []
    for m in re.finditer(
        r'<tr data-key="(\d+)">.*?data-col-date[^>]*>([^<]+)</td>.*?'
        r'data-col-main[^>]*><a href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.S,
    ):
        key, date, href, title = m.groups()
        out.append(
            {
                "id": key,
                "date": normalize_date(date.strip()),
                "href": href if href.startswith("http") else BIP_BASE + href,
                "title": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip(),
            }
        )
    return out


_TITLE_RE = re.compile(
    r"^(Interpelacja|Zapytanie|Wniosek)\b(.*)", re.I
)


def _przedmiot_from_title(title: str) -> str:
    """Przedmiot = segment po 'w sprawie' z tytułu."""
    m = re.search(r"\bw sprawie\b\s+(.*)$", title, re.I | re.S)
    return re.sub(r"\s*\([A-Z0-9.]+\)\s*$", "", m.group(1).strip()) if m else ""


_FILE_RE = re.compile(
    r'<a class="post-attach-link[^"]*"\s+href="(/attachment/[^"]+)"[^>]*>\s*(.*?)\s*</a>',
    re.S,
)
_METRYKA_RE = re.compile(
    r"Osoba, która odpowiada za treść:\s*<span>(.*?)</span>", re.S
)
_METRYKA_RE2 = re.compile(r"Osoba, która odpowiada za treść:\s*<span>(.*?)</span>", re.S)


def parse_detail(html: str, item: dict) -> dict | None:
    if not html:
        return None
    files = [
        (re.sub(r"<[^>]+>", " ", label).strip(), BIP_BASE + href.strip())
        for href, label in _FILE_RE.findall(html)
    ]

    title = item["title"]
    m = _TITLE_RE.match(title)
    typ_raw = m.group(1).lower() if m else "wniosek"
    typ = "interpelacja" if "interpelacj" in typ_raw else (
        "zapytanie" if "zapytan" in typ_raw else "wniosek"
    )

    # autor z metryki (mianownik) — najbardziej wiarygodny; potem tytuł
    mm = _METRYKA_RE2.search(html)
    author_raw = mm.group(1) if mm else ""
    if not author_raw:
        tm = re.search(r"Wniosek|Interpelacja|Zapytanie\s+(.*?)\s+(?:Radn|w sprawie)", title)
        author_raw = ""
    radny = _canonical_radny(author_raw)

    przedmiot = _przedmiot_from_title(title)

    # cri: numer w tytule (BOR.0003.14.2026) albo data jako fallback
    cri_m = re.search(r"\(([A-Za-z0-9.]+)\)\s*$", title)
    cri = cri_m.group(1) if cri_m else item["id"]

    rok = int(item["date"][:4]) if item["date"] else 0

    tresc_url, odpowiedz_url = "", ""
    for label, href in files:
        low = label.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif not tresc_url:
            tresc_url = href
    if not tresc_url and files:
        tresc_url = files[0][1]

    # data odpowiedzi z załącznika z "Odpowiedź"
    data_odp = ""
    if odpowiedz_url:
        tm = re.search(
            r'<a class="post-attach-link[^"]*"[^>]*href="[^"]*"[^>]*>\s*'
            r'Odpowied[^<]*</a>.*?datetime="(\d{4}-\d{2}-\d{2})',
            html, re.S | re.I
        )
        if tm:
            data_odp = tm.group(1)

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": item["date"],
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odp,
        "bip_url": BIP_BASE + item["href"] if not item["href"].startswith("http") else item["href"],
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji/zapytań/wniosków radnych z BIP Ostródy"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--max-pages", type=int, default=MAX_PAGES, help="Limit stron listingu"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też starsze kategorie; domyślnie tylko 2024-2029",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else 2024

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Ostródy (kategoria 1010) ===")
    items: dict[str, dict] = {}
    empty_streak = 0
    page = 1
    while page <= args.max_pages:
        html = fetch_listing(session, page)
        rows = parse_listing(html)
        new = [r for r in rows if r["id"] not in items]
        _log(f"  strona {page}: {len(rows)} wierszy, nowych: {len(new)}")
        if not new:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        for r in new:
            items[r["id"]] = r
        if page % 5 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(items)} znalezionych)")
        page += 1
    print(f"  Listing: {len(items)} rekordów w kategorii")

    records = []
    for item in items.values():
        detail_html = fetch_text(session, item["href"])
        if not detail_html:
            print(f"  [skip] brak treści: {item['href']}")
            continue
        rec = parse_detail(detail_html, item)
        if not rec:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    records.sort(key=lambda r: (r["typ"] != "interpelacja"))

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    wni = sum(1 for r in records if r["typ"] == "wniosek")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Wnioski:       {wni}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
