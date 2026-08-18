#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Kutno (eSesja.pl).

Źródło: platforma eSesja — moduł "Interpelacje i zapytania" JEST AKTYWNY.

    https://kutno.esesja.pl/interpelacje_i_zapytania/1   (paginacja, 20/s)

Struktura listingu (każdy .user-item):
    <a href="/interpelacja/{id}_{hash}/{slug}.htm"><strong>{tytuł}</strong></a>
    <a href="/radny/{uid}/{imie-nazwisko}.htm">{Imię Nazwisko}</a> - {Typ} z dnia {data}

Detal /interpelacja/{id}_{hash}/{slug}.htm:
    Autor: <a href="/radny/...">{Imię Nazwisko}</a>, dodano: ...
    wpis: {pełny tytuł / przedmiot}
    Załącznik: <a class='wiecej' href="/interpelacje/{n}/{id}/{hash}.pdf">Pobierz plik</a>

eSesja nie udostępnia osobnego pola/załącznika "Odpowiedź" (odpowiedz_status
zawsze "Nie udzielono", odpowiedz_url pusty).

Output: rekordy w formacie Radoskop (schema interpelacje.json).
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/x
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all
"""

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://kutno.esesja.pl"
REGISTER = f"{BASE}/interpelacje_i_zapytania"
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.4
MAX_PAGES = 100
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


from difflib import SequenceMatcher


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _match_radny(radny):
    """Dopasowuje 'Robert Marcin Stępniewski' (eSesja, pełne imię) do klucza
    config (np. 'Robert Stępniewski'). Strategia: zgadza się pierwsze imię +
    nazwisko (bez drugiego imienia)."""
    r = (radny or "").strip()
    if not r:
        return r
    if r in _CLUB_ASSIGN:
        return r
    rt = r.lower().split()
    # dopasuj po pierwszym imieniu i nazwisku (ostatni token)
    surname = rt[-1]
    first = rt[0]
    best, bestscore = "", 0.0
    for name in _CLUB_ASSIGN:
        nt = name.lower().split()
        if not nt:
            continue
        # nazwisko musi się zgadzać; pierwsze imię też
        if nt[-1] != surname:
            continue
        if nt[0] == first:
            # pierwsze imię się zgadza
            score = 1.0 - abs(len(rt) - len(nt)) * 0.05
            if score > bestscore:
                best, bestscore = name, score
            continue
        # miękkie dopasowanie nazwiska
        sc = SequenceMatcher(None, nt[-1], surname).ratio()
        if sc >= 0.75 and sc > bestscore:
            best, bestscore = name, sc
    return best if bestscore >= 0.75 else radny


def _club_for(radny):
    canon = _match_radny(radny)
    code = _CLUB_ASSIGN.get(canon, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(4)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Parsing listingu
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(
    r'<a href="(/interpelacja/[^"]+)"[^>]*><strong>(.*?)</strong></a>'
    r'[\s\S]*?<a href="(/radny/[^"]+)"[^>]*>(.*?)</a>\s*-\s*'
    r'([^<]+?)<',
    re.S,
)


def parse_listing(html):
    """Zwraca listę dictów z listingu: {detail_url, przedmiot, radny, typ, data_str}."""
    out = []
    for m in _LIST_ITEM_RE.finditer(html):
        detail = m.group(1)
        tytul = _clean(m.group(2))
        radny = _clean(m.group(4))
        sub = _clean(m.group(5))
        # sub = "{Typ} z dnia {data}" (np. "Interpelacja z dnia 13 sierpnia 2026")
        typ = "interpelacja"
        ms = re.search(r"^(Interpelacja|Zapytanie|Wniosek|Petycja)", sub, re.I)
        if ms:
            t = ms.group(1).lower()
            if t == "zapytanie":
                typ = "zapytanie"
            elif t == "wniosek":
                typ = "wniosek"
        # data "z dnia 13 sierpnia 2026"
        data_raw = re.search(r"z dnia\s+(.+)$", sub, re.I)
        data_str = data_raw.group(1).strip() if data_raw else ""
        out.append({
            "detail_url": urljoin(BASE, detail),
            "przedmiot": unescape(tytul),
            "radny": radny,
            "typ": typ,
            "data_str": data_str,
        })
    return out


_MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}


def _pl_date_to_iso(data_str):
    """'13 sierpnia 2026' -> '2026-08-13'."""
    m = re.match(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", data_str or "", re.I)
    if not m:
        return ""
    dd = int(m.group(1))
    mon = _MONTHS_PL.get(m.group(2).lower())
    if not mon:
        return ""
    return f"{m.group(3)}-{mon:02d}-{dd:02d}"


# ---------------------------------------------------------------------------
# Parsing detalu (załącznik PDF = treść)
# ---------------------------------------------------------------------------

_PDF_RE = re.compile(r"""href=['"]([^'"]*interpelacje/[^'"]+\.pdf)['"]""")


def parse_detail(html, listing):
    """Zwraca tresc_url (PDF). eSesja nie ma osobnej odpowiedzi."""
    tresc_url = ""
    for m in _PDF_RE.finditer(html):
        u = m.group(1)
        tresc_url = urljoin(BASE, u)
        break
    return tresc_url


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Kutno (eSesja)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = wszystkie")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też wcześniejsze kadencje; domyślnie tylko 2024-2029")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Kutno (eSesja) ===")
    seen = {}
    n_pages = 0
    page = 1
    while page <= MAX_PAGES:
        n_pages += 1
        url = f"{REGISTER}/{page}" if page > 1 else REGISTER
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            break
        items = parse_listing(html)
        new = 0
        for it in items:
            if it["detail_url"] not in seen:
                seen[it["detail_url"]] = it
                new += 1
        print(f"  strona {page}: {len(items)} rekordów (nowych {new}, łącznie {len(seen)})")
        # następna strona z pager'a
        m_next = re.search(r"<li class=['\"]?next['\"]?><a href=['\"]([^'\"]*interpelacje_i_zapytania/\d+)['\"]", html)
        if not m_next or (args.max_pages and n_pages >= args.max_pages):
            break
        nxt = int(re.search(r"/(\d+)$", m_next.group(1)).group(1))
        if nxt <= page:
            break
        page = nxt
    print(f"  stron: {n_pages} | unikalnych rekordów: {len(seen)}")

    records = []
    for i, (url, it) in enumerate(seen.items(), 1):
        dhtml = fetch_text(session, url)
        tresc_url = parse_detail(dhtml, it) if dhtml else ""
        data_wplywu = _pl_date_to_iso(it["data_str"])
        rok = int(data_wplywu[:4]) if data_wplywu and data_wplywu[:4].isdigit() else 0
        if min_rok and rok and rok < min_rok:
            continue
        m_id = re.search(r"/interpelacja/(\d+)_", url)
        canon_radny = _match_radny(it["radny"])
        records.append({
            "cri": m_id.group(1) if m_id else f"kutno-{i}",
            "typ": it["typ"],
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else ("2018-2023" if rok else ""),
            "radny": canon_radny,
            "przedmiot": it["przedmiot"],
            "data_wplywu": data_wplywu,
            "klub": _club_for(it["radny"]),
            "odpowiedz_status": "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": "",
            "data_odpowiedzi": "",
            "bip_url": url,
        })
        if i % 40 == 0:
            print(f"  ... {i}/{len(seen)}")
        time.sleep(DELAY)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    with_rad = sum(1 for r in records if r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | z radnym: {with_rad} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
