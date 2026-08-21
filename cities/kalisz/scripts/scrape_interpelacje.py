#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Kalisza (IX kad. 2024-2029).

Źródło: eSesja (https://kalisz.esesja.pl) — moduł "Interpelacje i zapytania".

    https://kalisz.esesja.pl/interpelacje_i_zapytania/{page}

Struktura listingu (paginowany — patrz _last_page / pager):
  Każdy rekord = <div class="user-item">:
      <p class="title"><a href="/interpelacja/{id}_{hash}/{slug}.htm">{przedmiot}</a></p>
      <p class="subtitle">{Radny} - Interpelacja z dnia {DD miesiąc YYYY}</p>
  W tym źródle TYTUŁ listingu = przedmiot (temat interpelacji) wprost.
  Typ (interpelacja/zapytanie) i data z subtitle; radny wprost.

  Detal (kodowanie URL jak eSesja — UTF-8 czytane jako latin-1):
      <div class='interpelacja_header'>... {Nr sprawy} ({przedmiot}) ...</div>
      <div class='wpis'><p>{pełna treść interpelacji}</p></div>
      <div class='iinfo'>{data} - <b>Radny</b></div>
  Kalisz NIE publikuje na tym module osobnych odpowiedzi (brak bloków
  'Odpowiedź'/'Załącznik' i PDF-ów) -> odpowiedz_status="Nie udzielono"
  dla wszystkich (uczciwa luka źródła).

Klub radnego z config.json (club_assignments -> clubs, fuzzy do mianownika).
Dedupe po bip_url (id z URL detalu). Rekordy < 2024 odrzucane (--all dla starszych).
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://kalisz.esesja.pl"
LIST_URL = f"{BASE}/interpelacje_i_zapytania"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for(radny):
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_nominative(parsed):
    if not parsed:
        return ""
    best, best_ratio = "", 0.0
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else ""


def _fix_url(url):
    try:
        return url.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return url


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    url2 = _fix_url(url)
    for attempt in range(3):
        try:
            resp = session.get(url2, timeout=40)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url2}: {e}")
            time.sleep(2)
    return ""


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


_SUB_RE = re.compile(
    r"^(?P<radny>.+?)\s*-\s*(?P<typ>interpelacj[ae]|zapytani[ae]|wniosek)\s+z dnia\s+"
    r"(?P<dd>\d{1,2})\s+(?P<mon>[a-ząćęłńóśźż]+)\s+(?P<yy>20\d{2})",
    re.I,
)


def _last_page(soup) -> int:
    best = 1
    for a in soup.select("ul.pager a"):
        m = re.search(r"/interpelacje_i_zapytania/(\d+)$", a.get("href", ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def parse_list_items(soup):
    items = []
    for item in soup.select("div.user-item"):
        a = item.select_one("p.title a[href*='/interpelacja/']")
        if not a:
            continue
        href = a.get("href")
        title = _clean(a.get_text(" ", strip=True))
        sub_el = item.select_one("p.subtitle")
        sub = _clean(sub_el.get_text(" ", strip=True)) if sub_el else ""
        m = _SUB_RE.search(sub)
        if not m:
            _log(f"  [parse] brak danych w subtitle: {sub!r}")
            continue
        typ_raw = m.group("typ")
        if typ_raw.lower().startswith("zapytani"):
            typ = "zapytanie"
        elif typ_raw.lower().startswith("wniosek"):
            typ = "wniosek"
        else:
            typ = "interpelacja"
        rok = int(m.group("yy"))
        data_wplywu = f"{rok}-{_MONTHS.get(m.group('mon').lower(), 0):02d}-{int(m.group('dd')):02d}"
        radny_raw = _clean(m.group("radny"))
        collective = (", " in sub) or (" i " in radny_raw) or ("oraz" in radny_raw.lower())
        if collective:
            radny, klub = "", ""
        else:
            matched = _match_nominative(radny_raw)
            radny = matched if matched else radny_raw
            klub = _club_for(matched) if matched else ""
        items.append({
            "href": href, "typ": typ, "radny": radny, "klub": klub,
            "data_wplywu": data_wplywu, "rok": rok, "przedmiot": title,
        })
    return items


_WPIS_RE = re.compile(
    r"<div class=['\"]wpis['\"]><p>(?P<txt>.*?)</p></div>\s*"
    r"<div class=['\"]iinfo['\"]>(?P<info>.*?)</div>",
    re.S,
)


def detail_pdfs(session, url):
    """Zwraca (tresc_url, odpowiedz_url, data_odpowiedzi). Kalisz nie publikuje PDF-ów."""
    html = fetch_text(session, url)
    if not html:
        return "", "", ""
    tresc_url, odpowiedz_url, data_odp = "", "", ""
    for m in _WPIS_RE.finditer(html):
        txt = _clean(m.group("txt"))
        info = m.group("info")
        low_txt = txt.lower()
        if "odpowiedź" in low_txt or "odpowiedz" in low_txt:
            hm = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", info)
            if hm and not odpowiedz_url:
                odpowiedz_url = _fix_url(hm.group(1))
                if not odpowiedz_url.startswith("http"):
                    odpowiedz_url = BASE + odpowiedz_url
                dm = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", info)
                if dm:
                    mo = _MONTHS.get(dm.group(2).lower())
                    if mo:
                        data_odp = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
    return tresc_url, odpowiedz_url, data_odp


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Kalisz (eSesja)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None, help="Ogranicz liczbę stron (testy)")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Kalisz (eSesja) ===")
    html = fetch_text(session, LIST_URL)
    soup = BeautifulSoup(html, "html.parser")
    total_pages = _last_page(soup)
    pages = min(total_pages, args.max_pages) if args.max_pages else total_pages
    print(f"  stron listingu: {total_pages} (przetwarzam {pages})")

    items = []
    items.extend(parse_list_items(soup))
    for page in range(2, pages + 1):
        time.sleep(DELAY)
        ph = fetch_text(session, f"{LIST_URL}/{page}")
        if not ph:
            print(f"  [skip] strona {page} brak treści")
            continue
        items.extend(parse_list_items(BeautifulSoup(ph, "html.parser")))

    seen_url = set()
    uniq_items = []
    for it in items:
        u = it["href"]
        if u in seen_url:
            continue
        seen_url.add(u)
        uniq_items.append(it)
    items = uniq_items
    print(f"  rekordów w listingach (po dedupe): {len(items)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    records = []
    for it in items:
        if min_rok and it["rok"] < min_rok:
            continue
        bip_url = _fix_url(it["href"] if it["href"].startswith("http") else BASE + it["href"])
        idm = re.search(r"/interpelacja/([^/]+)/", bip_url)
        cri = f"cri-kalisz-{idm.group(1)}" if idm else f"cri-kalisz-{len(records)}"
        tresc_url, odpowiedz_url, data_odp = detail_pdfs(session, bip_url)
        rec = {
            "cri": cri,
            "typ": it["typ"],
            "rok": it["rok"],
            "kadencja": "2024-2029" if it["rok"] >= 2024 else "2018-2024",
            "radny": it["radny"],
            "przedmiot": it["przedmiot"] or "",
            "data_wplywu": it["data_wplywu"],
            "klub": it["klub"],
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": data_odp,
            "bip_url": bip_url,
        }
        records.append(rec)
        time.sleep(DELAY)

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)

    seen = set()
    final = []
    for r in records:
        if r["bip_url"] in seen:
            continue
        seen.add(r["bip_url"])
        final.append(r)
    records = final

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in records if not r["radny"])
    no_subj = sum(1 for r in records if not r["przedmiot"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Bez radnego: {no_radny} | Bez przedmiotu: {no_subj} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
