#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Człuchowie.

Źródło: eSesja (https://czluchow.esesja.pl) — moduł "Interpelacje i zapytania".

    https://czluchow.esesja.pl/interpelacje_i_zapytania

Struktura listingu (kadencja 2024-2029, wszystkie rekordy na jednej stronie):
  Każdy rekord = <div class="user-item">:
      <p class="title"><a href="/interpelacja/{id}_{hash}/{slug}.htm">{przedmiot}</a></p>
      <p class="subtitle">{Radny} - {Typ} z dnia {DD miesiąc YYYY}</p>
  Typ: Interpelacja / Zapytanie / Wniosek.

  Detal (z kodowaniem URL — strona nadaje UTF-8 po konwersji latin-1):
      <div class="wpis"><p>Załącznik do Interpelacja ({filename}.pdf)</p></div>
      <div class="iinfo">...<a class="wiecej" href="/interpelacje/{id}/{per}/{hash}.pdf">Pobierz plik</a></div>
    Plik odpowiedzi ma w nazwie "odpowiedz".

Klub radnego z config.json (club_assignments -> clubs, fuzzy do mianownika).

Output: rekordy Radoskop. Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://czluchow.esesja.pl"
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
    best, best_ratio = "", 0.0
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else ""


def _fix_url(url: str) -> str:
    """eSesja wysyła UTF-8 bajty, które czytamy jako latin-1 -> naprawiamy."""
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
            resp = session.get(url2, timeout=30)
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
    return re.sub(r"\s+", " ", s).strip()


_SUB_RE = re.compile(
    r"^(?P<radny>.+?)\s*-\s*(?P<typ>Interpelacj[ae]|Zapytani[ae]|Wniosek)\s+z dnia\s+"
    r"(?P<dd>\d{1,2})\s+(?P<mon>[a-ząćęłńóśźż]+)\s+(?P<yy>20\d{2})",
    re.I,
)


def parse_list_items(soup, session):
    items = []
    for item in soup.select("div.user-item"):
        a = item.select_one("p.title a[href*='/interpelacja/']")
        if not a:
            continue
        href = a.get("href")
        przedmiot = _clean(a.get_text(" ", strip=True))
        sub = _clean(item.select_one("p.subtitle").get_text(" ", strip=True)) if item.select_one("p.subtitle") else ""
        m = _SUB_RE.search(sub)
        if not m:
            _log(f"  [parse] brak danych w subtitle: {sub!r}")
            continue
        typ_raw = m.group("typ")
        typ = "zapytanie" if typ_raw.lower().startswith("zapytani") else \
              ("wniosek" if typ_raw.lower().startswith("wniosek") else "interpelacja")
        rok = int(m.group("yy"))
        data_wplywu = f"{rok}-{_MONTHS.get(m.group('mon').lower(), 0):02d}-{int(m.group('dd')):02d}"
        radny_gen = _clean(m.group("radny"))
        matched = _match_nominative(radny_gen)
        radny = matched if matched else radny_gen
        klub = _club_for(matched) if matched else ""
        items.append({
            "href": href, "przedmiot": przedmiot, "typ": typ, "radny": radny,
            "data_wplywu": data_wplywu, "rok": rok, "klub": klub,
        })
    return items


_WPIS_RE = re.compile(
    r"<div class=['\"]wpis['\"]><p>[^<]*\((?P<name>[^()]*?\.pdf)\)</p></div>\s*"
    r"<div class=['\"]iinfo['\"]>.*?<a class=['\"]wiecej['\"] href=['\"](?P<href>[^'\"]+\.pdf)['\"]",
    re.S,
)


def detail_pdfs(session, url):
    html = fetch_text(session, url)
    if not html:
        return "", ""
    tresc_url, odpowiedz_url = "", ""
    for m in _WPIS_RE.finditer(html):
        name, href = m.group("name").lower(), m.group("href")
        full = href if href.startswith("http") else BASE + href
        if "odpowiedz" in name or "odpowiedź" in name:
            if not odpowiedz_url:
                odpowiedz_url = full
        else:
            if not tresc_url:
                tresc_url = full
    return tresc_url, odpowiedz_url


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Człuchów (eSesja)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Człuchów (eSesja) ===")
    html = fetch_text(session, LIST_URL)
    soup = BeautifulSoup(html, "html.parser")
    items = parse_list_items(soup, session)
    print(f"  rekordów w listingu: {len(items)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    records = []
    for i, it in enumerate(items, 1):
        if min_rok and it["rok"] < min_rok:
            continue
        bip_url = _fix_url(it["href"] if it["href"].startswith("http") else BASE + it["href"])
        tresc_url, odpowiedz_url = detail_pdfs(session, bip_url)
        rec = {
            "cri": f"cri-czluchow-{i}",
            "typ": it["typ"],
            "rok": it["rok"],
            "kadencja": "2024-2029" if it["rok"] >= 2024 else "2018-2024",
            "radny": it["radny"],
            "przedmiot": it["przedmiot"],
            "data_wplywu": it["data_wplywu"],
            "klub": it["klub"],
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "bip_url": bip_url,
        }
        records.append(rec)
        time.sleep(DELAY)

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
