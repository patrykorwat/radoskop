#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Sławnie (IX kad. 2024-2029).

Źródło: BIP Sławna (https://bip.slawno.pl), sekcja "Rada Miejska > Interpelacje i zapytania"
(artykuł /artykuly/interpelacje-i-zapytania) z podkategoriami rocznymi (2024, 2025).

Struktura:
  * Strona główna -> linki do podkategorii rocznych (etykiety "2024"/"2025"/"2026").
  * Podkategoria roczna -> lista linków detali: /artykul/interpelacja-z-dnia-{DD}-{miesiąc}-{YYYY}-roku
    (oraz zapytanie-z-dnia-..., wniosek-...).
  * Detal -> h1 tytuł "Interpelacja z dnia {DD miesiąc YYYY} roku"; metryczka artykułu
    "Wytworzył: {radny}" (autor); załączniki (linki /attachments/...):
      - "Interpelacja z dnia ..." -> tresc_url,
      - "Odpowiedź ..." -> odpowiedz_url.
    Przedmiot NIE jest publikowany w HTML (tylko w skanach PDF) -> pozostawiamy pusty
    (nie fabrykujemy). Data z tytułu; autor z metryczki (ostatnie "Wytworzył"), fuzzy do config.

Klub radnego z config.json (club_assignments -> clubs). Dedupe po tresc_url.
Rekordy < 2024 odrzucane (--all dla starszych).
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

BASE = "https://bip.slawno.pl"
MAIN_URL = f"{BASE}/artykuly/interpelacje-i-zapytania"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
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


def _match_nominative(name):
    if not name:
        return ""
    name = name.strip()
    if name in _CLUB_ASSIGN:
        return name
    best, best_ratio = "", 0.0
    for cand in _CLUB_ASSIGN:
        r = SequenceMatcher(None, name.lower(), cand.lower()).ratio()
        if r > best_ratio:
            best_ratio, best = r, cand
    return best if best_ratio >= 0.75 else name


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=40)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def year_links(session):
    """Podkategorie roczne z głównej strony (etykiety 2024/2025/2026)."""
    html = fetch_text(session, MAIN_URL)
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("main") or soup
    out = []
    for a in el.find_all("a", href=True):
        lab = _clean(a.get_text(" ", strip=True))
        if re.fullmatch(r"20\d\d", lab):
            out.append({"rok": int(lab), "url": a["href"] if a["href"].startswith("http") else BASE + a["href"]})
    return out


_DETAIL_LINK_RE = re.compile(r'<a[^>]+href="(/artykul/(?:interpelacja|zapytanie|wniosek)-[^"]+)"[^>]*>')

def detail_links(session, year_url):
    html = fetch_text(session, year_url)
    out = []
    for m in _DETAIL_LINK_RE.finditer(html):
        h = m.group(1)
        if h not in out:
            out.append(h)
    return [BASE + h for h in out]


_TITLE_DATE_RE = re.compile(
    r"(?:interpelacja|zapytanie|wniosek)\s+z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", re.I
)


def parse_detail(session, url):
    html = fetch_text(session, url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    title = _clean((main.find(["h1", "h2"]) or soup.find(["h1", "h2"])).get_text(" ", strip=True)) if (main.find(["h1", "h2"]) or soup.find(["h1", "h2"])) else ""
    # data z tytułu
    m = _TITLE_DATE_RE.search(title)
    data_wplywu = ""
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            data_wplywu = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    rok = int(data_wplywu[:4]) if data_wplywu else 0
    # autor: ostatnie "Wytworzył:" (metryczka artykułu po załącznikach)
    autor = ""
    mainstr = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(main)))
    for a in re.finditer(r"Wytworzy[łl]:\s*([^D]{2,60}?)\s*Data wytworzenia", mainstr):
        autor = _clean(a.group(1))
    radny = _match_nominative(autor)
    klub = _club_for(radny) if radny and radny in _CLUB_ASSIGN else ""
    # załączniki
    tresc_url, odpowiedz_url = "", ""
    for ab in main.select("div.attachments_bar"):
        for a in ab.select("a[href*='/attachments/']"):
            href = a["href"]
            name_el = a.select_one(".attachment-name-details__name")
            name = _clean(name_el.get_text(" ", strip=True)) if name_el else ""
            aurl = href if href.startswith("http") else BASE + href
            if name.lower().startswith("odpowiedź"):
                if not odpowiedz_url:
                    odpowiedz_url = aurl
            else:
                if not tresc_url:
                    tresc_url = aurl
    # typ z tytułu/URL
    typ = "interpelacja"
    tl = (title + " " + url).lower()
    if "zapytanie" in tl:
        typ = "zapytanie"
    elif "wniosek" in tl:
        typ = "wniosek"
    if not tresc_url:
        _log(f"  [slawno] brak tresc_url: {url}")
        return None
    idm = re.search(r"/artykul/([a-z0-9-]+)", url)
    cri = f"cri-slawno-{idm.group(1)}" if idm else f"cri-slawno-{rok}"
    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": "",
        "data_wplywu": data_wplywu,
        "klub": klub,
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Sławno (BIP detale)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Sławno (BIP detale) ===")
    years = year_links(session)
    years = [y for y in years if y["rok"] >= MIN_ROK_DEFAULT] if not args.all else years
    print("  lata:", [y["rok"] for y in years])

    records = []
    for y in years:
        links = detail_links(session, y["url"])
        n = links[:args.max_pages] if args.max_pages else links
        print(f"  rok {y['rok']}: {len(links)} detali (przetwarzam {len(n)})")
        for u in n:
            rec = parse_detail(session, u)
            if rec:
                records.append(rec)
            time.sleep(DELAY)

    seen, final = set(), []
    for r in records:
        if r["tresc_url"] in seen:
            continue
        seen.add(r["tresc_url"])
        final.append(r)
    records = sorted(final, key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

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
