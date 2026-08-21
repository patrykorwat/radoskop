#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Włocławek (IX kad. 2024-2029).

Źródło: BIP Włocławka (bip.wloclawek.eu, CMS CCT — ten sam co BIP Przemyśla).
eSesja (wloclawek.esesja.pl) — moduł interpelacji NIEAKTYWNY ("Brak aktywności").

    https://bip.wloclawek.eu/2673/interpelacje-sesyjne-radnych-rady-miasta.html
    https://bip.wloclawek.eu/2721/interpelacje-miedzysesyjne-radnych-rady-miasta.html

Struktura (CCT, paginacja `?Page=N&cct-search=&is_content_type_search=1`, 10/stronę):
  Listing: <div class="cct-item__name"><a href="{id}/interpelacja-...html">Tytuł</a>
           <div class="cct-attribute--162">Imię i nazwisko Radnego
           <div class="cct-attribute--159">Temat Interpelacji
           <div class="cct-attribute--150">Status Realizacji
  Detal:   <div class="cct-page__name">Imię i nazwisko Radnego:</div><div class="cct-page__value">…
           (… Numer Interpelacji / Temat / Status Realizacji / Kadencja Rady /
              Numer Sesji / Rok)  oraz załączniki
           <a href="/download/attachment/…">Interpelacja nr N (PDF | 40,50KB)</a>
           <a href="/download/attachment/…">Odpowiedź na interpelację (PDF | 83,25KB)</a>

  Data złożenia z tytułu ("złożona w dniu 24 czerwca 2026 r."). Kadencja/Rok z detalu
  (filtrujemy do IX kad. 2024-2029). radny z "Imię i nazwisko Radnego", klub z config.json
  (club_assignments -> clubs). Dedupe po bip_url (id z URL detalu).
Rekordy < 2024 odrzucane (--all dla starszych kadencji).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.wloclawek.eu"
LIST_URLS = [
    f"{BASE}/2673/interpelacje-sesyjne-radnych-rady-miasta.html",
    f"{BASE}/2721/interpelacje-miedzysesyjne-radnych-rady-miasta.html",
]
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.4
MAX_PAGES_PER_LIST = 200
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


def _club_for_radny(radny):
    code = _CLUB_ASSIGN.get(radny, "")
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
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


# Lista: linki do detali
def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select(".cct-item__name a[href]"):
        u = a.get("href")
        if u and u not in out:
            out.append(u)
    return out


_ATTR_RE = re.compile(
    r'<div class="cct-page__name">\s*(.*?)\s*</div>\s*'
    r'<div class="cct-page__value">\s*(.*?)\s*</div>',
    re.S,
)
_FILE_RE = re.compile(
    r'<a[^>]+href="((?:https?://[^"]*)?/download/attachment/[^"]+)"[^>]*>(.*?)</a>', re.S
)

_DATE_IN_TITLE_RE = re.compile(
    r"złożon[ae]?\s+w\s+dniu\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", re.I
)


def _date_from_polish(s):
    if not s:
        return "", 0
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", s)
    if not m:
        return "", 0
    mo = _MONTHS.get(m.group(2).lower())
    if not mo:
        return "", 0
    rok = int(m.group(3))
    return f"{rok}-{mo:02d}-{int(m.group(1)):02d}", rok


def parse_detail(html, bip_url):
    if not html:
        return None
    attrs = {}
    for name, value in _ATTR_RE.findall(html):
        attrs[_clean(name)] = _clean(value)

    radny = attrs.get("Imię i nazwisko Radnego:", "")
    temat = attrs.get("Temat Interpelacji:", attrs.get("Temat Zapytania:", ""))
    nr = attrs.get("Numer Interpelacji:", attrs.get("Numer Zapytania:", ""))
    rok_str = attrs.get("Rok:", "")
    kad = attrs.get("Kadencja Rady:", "")
    status_real = attrs.get("Status Realizacji:", "")

    try:
        rok = int(rok_str)
    except (TypeError, ValueError):
        rok = 0

    header_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    header = _clean(header_m.group(1)) if header_m else ""

    files = []
    for href, label in _FILE_RE.findall(html):
        if not href.startswith("http"):
            href = BASE + href
        files.append((_clean(label), href))

    # typ
    hay = " ".join([header, temat]).lower()
    typ = "zapytanie" if "zapytanie" in hay else "interpelacja"

    # data złożenia z tytułu (np. "złożona w dniu 24 czerwca 2026 r.")
    data_wplywu, _rok_t = _date_from_polish(header)

    tresc_url, odpowiedz_url = "", ""
    for label, href in files:
        low = label.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif ("interpelacj" in low or "zapytan" in low) and not tresc_url:
            tresc_url = href
        elif not tresc_url and not odpowiedz_url:
            tresc_url = href
    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    kadencja = "2024-2029" if "IX" in kad or rok >= 2024 else "2018-2024"

    return {
        "cri": nr or bip_url,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": temat,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": bip_url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Włocławek (BIP CCT)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None, help="Ogranicz paginację (testy)")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = 0 if args.all else MIN_ROK_DEFAULT
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Włocławek (BIP CCT) ===")
    seen = {}
    for li_url in LIST_URLS:
        page = 1
        empty = 0
        while page <= MAX_PAGES_PER_LIST:
            url = f"{li_url}?Page={page}&cct-search=&is_content_type_search=1"
            if page == 1:
                url = li_url
            time.sleep(DELAY)
            html = fetch_text(session, url)
            links = parse_listing(html)
            new = [u for u in links if u not in seen]
            _log(f"  {Path(li_url).stem} strona {page}: {len(links)} linków, nowych {len(new)}")
            if not new:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
            for u in new:
                seen[u] = ""
            if args.max_pages and page >= args.max_pages:
                break
            page += 1
        print(f"  [{Path(li_url).stem}] do str. {page}")

    print(f"  Listing: {len(seen)} unikalnych detali")
    records = []
    for i, url in enumerate(seen, 1):
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if i % 100 == 0:
            print(f"  szczegóły: {i}...")
    records.sort(key=lambda r: (r["data_wplywu"] or "", r["bip_url"]), reverse=True)

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
    print(f"Zapisano: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
