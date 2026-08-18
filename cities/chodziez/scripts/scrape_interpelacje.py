#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Chodzieży.

Źródło: BIP Chodzieży — "Interpelacje i zapytania radnych" (strona w ramach
rejestru sesji/protokołów):

    https://bip.chodziez.pl/chodziezm/bip/organy-wladzy-publicznej/rada-miejska/interpelacje-i-zapytania-radnych-po-protokolach-sesji.html

eSesja (https://chodziez.esesja.pl) — moduł interpelacje "Brak aktywności lub
moduł nieaktywny" (uczciwa luka).

Struktura:
  Strona to ciąg bloków. Każdy blok zaczyna się nagłówkiem
    <h2>Interpelacja radnego/radnej {Imię Nazwisko} z dnia {DD miesiąc YYYY} r.</h2>
  i kończy metryką `content-footer` ("Osoba odpowiedzialna..."). Wewnątrz bloku
  jest jedna lub więcej linii <p>:
    <p><a href="...rm-0003-N-YYYY-...pdf">Interpelacja N/YYYY</a>; [<a href="...odp.pdf">odpowiedź</a>]</p>
  Każda "Interpelacja N/YYYY" = osobny rekord; treść = pierwszy link,
  odpowiedź = link(i) "odpowiedź".

Klub radnego z config.json (club_assignments -> clubs).

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all
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

BASE_URL = "https://bip.chodziez.pl"
PAGE_URL = (BASE_URL + "/chodziezm/bip/organy-wladzy-publicznej/rada-miejska/"
            "interpelacje-i-zapytania-radnych-po-protokolach-sesji.html")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MIN_ROK_DEFAULT = 2024
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "luty": 2, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}

_H2_RE = re.compile(
    r"(?P<kind>Interpelacj[ae]|Zapytani[ae])\s+radn\w*\s+(?P<radny>.+?)\s+z dnia\s+"
    r"(?P<dd>\d{1,2})\s+(?P<mon>[a-ząćęłńóśźż]+)\s+(?P<yy>20\d{2})",
    re.I,
)
_LINK_RE = re.compile(r"(Interpelacj[ae]|Zapytani[ae])\s*(?:Nr\s*)?(\d+)/(20\d{2})", re.I)


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


def _clean_radny(raw: str) -> str:
    """Normalizuje odmienione nazwisko (genitiv, np. 'Ewy Siodły') do wpisu
    z config club_assignments (mianownik), dopasowując po nazwisku."""
    s = re.sub(r"\s+", " ", raw or "").strip()
    if not s:
        return ""
    if s in _CLUB_ASSIGN:
        return s
    surname = s.split()[-1].lower().rstrip(".")
    best, bestscore = "", 0.0
    for cname in _CLUB_ASSIGN:
        ct = cname.split()
        if not ct:
            continue
        score = SequenceMatcher(None, ct[-1].lower(), surname).ratio()
        if score > bestscore:
            bestscore, best = score, cname
    if best and bestscore >= 0.6:
        return best
    return s


def _club_for_radny(radny: str) -> str:
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
            time.sleep(DELAY)
            resp = session.get(url, timeout=30, verify=False)
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


def _resolve(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    # względny (zasoby/files/...) — względem katalogu strony
    return BASE_URL + "/chodziezm/bip/organy-wladzy-publicznej/rada-miejska/" + href


def parse_page(html: str, bip_url: str):
    """Zwraca rekordy z pojedynczej strony rejestru Chodzieży.

    Strona = ciąg bloków; każdy blok ma nagłówek <h2> z radnym i datą, a w
    środku linie <p> z linkami. Linia zawiera "Interpelacja/Zapytanie N/YYYY"
    (treść) oraz opcjonalnie link "Odpowiedź". Odpowiedź bywa w osobnej linii
    <p> po linii treści — stąd przetwarzanie sekwencyjne linii bloku.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    h2s = soup.find_all("h2")
    out = []
    seen = set()
    for h2 in h2s:
        m = _H2_RE.search(h2.get_text(" ", strip=True))
        if not m:
            continue
        kind_h2 = "zapytanie" if m.group("kind").lower().startswith("zapyt") else "interpelacja"
        radny = _clean_radny(m.group("radny"))
        dd, mon, yy = int(m.group("dd")), _MONTHS.get(m.group("mon").lower()), int(m.group("yy"))
        if not mon:
            continue
        data_wplywu = f"{yy:04d}-{mon:02d}-{dd:02d}"
        # kolekcja linii <p> bloku (do kolejnego <h2>)
        lines = []
        node = h2.find_next_sibling()
        while node and node.name != "h2":
            if node.name == "p":
                lines.append(node)
            node = node.find_next_sibling()
        # przetwarzanie sekwencyjne linii
        pending = None  # rekord z treścią, czeka na ewentualną odpowiedź
        for p in lines:
            anchors = [a for a in p.find_all("a", href=True)]
            if not anchors:
                continue
            for a in anchors:
                label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
                im = _LINK_RE.search(label)
                if im:
                    # start nowego rekordu
                    typ = "zapytanie" if im.group(1).lower().startswith("zapyt") else "interpelacja"
                    cri = f"{im.group(1).strip().capitalize()} {im.group(2)}/{im.group(3)}"
                    if cri in seen:
                        pending = None
                        continue
                    seen.add(cri)
                    rec = {
                        "cri": cri,
                        "typ": typ,
                        "rok": int(im.group(3)),
                        "kadencja": "2024-2029" if int(im.group(3)) >= 2024 else "2018-2023",
                        "radny": radny,
                        "przedmiot": label,
                        "data_wplywu": data_wplywu,
                        "klub": _club_for_radny(radny),
                        "odpowiedz_status": "Nie udzielono",
                        "tresc_url": _resolve(a["href"]),
                        "odpowiedz_url": "",
                        "data_odpowiedzi": "",
                        "bip_url": bip_url,
                    }
                    out.append(rec)
                    pending = rec
                    continue
                # linia/łańcuch odpowiedzi
                if pending and re.search(r"odpow", label, re.I):
                    if not pending["odpowiedz_url"]:
                        pending["odpowiedz_url"] = _resolve(a["href"])
                        pending["odpowiedz_status"] = "Udzielono"
                    pending = None
        # zamknij ewentualny pending (bez odpowiedzi)
        pending = None
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Chodzieży"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Chodzieży ===")
    html = fetch_text(session, PAGE_URL)
    if not html:
        print("  [skip] brak treści")
        return 1
    records = parse_page(html, PAGE_URL)
    if min_rok:
        records = [r for r in records if not r["rok"] or r["rok"] >= min_rok]

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
