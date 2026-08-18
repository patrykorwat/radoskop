#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Dębicy.

Źródło: BIP Dębicy (CMS bip.gov.pl) — kategoria "Zapytania i interpelacje radnych".

    https://debica.bip.gov.pl/zapytania-i-interpelacje-radnych/

eSesja (https://debica.esesja.pl) — moduł interpelacje sprawdzony, ale rejestr
prowadzony jest na BIP (artykuły per interpelacja/zapytanie), więc źródłem jest BIP.

Struktura:
  Kategoria to lista artykułów paginowana:
      /articles/index/zapytania-i-interpelacje-radnych            (strona 1)
      /articles/index/zapytania-i-interpelacje-radnych/page:N    (strony 2..N)
  Każda pozycja listingu = link do artykułu detalu:
      /zapytania-i-interpelacje-radnych/{slug}.html
  Detal artykułu:
    - <title> = typ + radny, np. "Interpelacja radnej Moniki Garduły"
    - data publikacji "YYYY-MM-DD" w metryce
    - body: "Interpelacja radnej {R} z dnia DD.MM.YYYY r. dotycząca {przedmiot}."
    - linki: treść PDF (/fobjects/download/{id}/...odpowiedz...) i odpowiedź PDF.

Klub radnego z config.json (club_assignments -> clubs).

Output: rekordy Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --max-pages 2   (test)
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

BASE = "https://debica.bip.gov.pl"
CATEGORY = "zapytania-i-interpelacje-radnych"
LIST_URL = f"{BASE}/articles/index/{CATEGORY}"
MIN_ROK_DEFAULT = 2024
MAX_PAGES_DEFAULT = 16

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> tuple[dict, dict]:
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_nominative(parsed: str) -> str:
    """Dopasuj imię w formie dopełniacza ('Aliny Rzewuskiej') do klucza
    mianownikowego z config.json ('Alina Rzewuska'); zwróć klucz lub ''."""
    from difflib import SequenceMatcher
    best, best_ratio = "", 0.0
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
        # bonus gdy pierwsze słowa się pokrywają
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    if best_ratio >= 0.6:
        return best
    return ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
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
            return ""  # 404 etc.
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def list_detail_urls(session: requests.Session, max_pages: int) -> list[str]:
    urls = []
    for page in range(1, max_pages + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}/page:{page}"
        html = fetch_text(session, url)
        if not html:
            _log(f"  strona {page}: pusta")
            break
        soup = BeautifulSoup(html, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # only detail article links under this category
            if f"/{CATEGORY}/" not in href:
                continue
            if re.search(r"/articles/index|/page:", href):
                continue
            if href.rstrip("/") == f"/{CATEGORY}" or href.endswith(f"/{CATEGORY}/"):
                continue
            urls.append(href if href.startswith("http") else BASE + href)
            found += 1
        # dedupe
        urls = list(dict.fromkeys(urls))
        _log(f"  strona {page}: +{found} linków, razem {len(urls)}")
        if found == 0:
            break
        time.sleep(DELAY)
    return urls


_DATE_RE = re.compile(r"z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")
_META_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_KIND_RE = re.compile(r"^(Interpelacj[ae]|Zapytani[ae]|Wniosek|O[o]dpowied[źz])\b", re.I)


def parse_detail(soup: BeautifulSoup, url: str) -> dict | None:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body_el = soup.find("article") or soup.find(class_=re.compile(r"tresc|content|article"))
    body = _clean(body_el.get_text("\n", strip=True)) if body_el else _clean(soup.get_text(" ", strip=True))

    # type
    m_kind = re.match(r"(Interpelacj[ae]|Zapytani[ae]|Wniosek)\b", title, re.I)
    if not m_kind:
        return None
    raw_kind = m_kind.group(1)
    typ = "zapytanie" if raw_kind.lower().startswith("zapytani") else \
          ("wniosek" if raw_kind.lower().startswith("wniosek") else "interpelacja")

    # radny: z "radnej/radnego/radni/radna {Imię Nazwisko}" (forma dopełniacza)
    m_rad = re.search(r"(?:radn\w+\s+)([A-ZĄĆĘŁŃÓŚŹŻ][\w\-']+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\-']+)?)", title)
    radny_gen = _clean(m_rad.group(1)) if m_rad else ""
    # dopasuj do mianownika z config (klucz club_assignments) — daje klub;
    # jeśli brak dopasowania, zostaw oryginalną formę (klub pozostanie pusty — uczciwie)
    _matched = _match_nominative(radny_gen) if radny_gen else ""
    radny = _matched if _matched else radny_gen
    klub = _club_for_radny(_matched) if _matched else ""

    # date
    data_wplywu = ""
    rok = 0
    m_d = _DATE_RE.search(body)
    if m_d:
        dd, mm, yy = m_d.group(1), m_d.group(2), m_d.group(3)
        data_wplywu = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
        rok = int(yy)
    else:
        m_m = _META_DATE_RE.search(body)
        if not m_m:
            m_m = re.search(r"\b(20\d{2})-\d{2}-\d{2}\b", body)
        if m_m:
            rok = int(m_m.group(1))
            data_wplywu = m_m.group(0) if m_m.re.groups == 3 else data_wplywu

    # przedmiot: po dacie ("dotycząca ..." / "w sprawie ...") do wzmianki o odpowiedzi
    przedmiot = ""
    m_dbody = _DATE_RE.search(body)
    if m_dbody:
        tail = body[m_dbody.end():]
        # do "Odpowiedź" / końca
        m_end = re.search(r"\sOdpowied[źz]|\sOdp\b", tail)
        if m_end:
            tail = tail[:m_end.start()]
        tail = re.sub(r"^\s*r?\.?\s*", "", tail)
        tail = re.sub(r"^\s*dotycz[ąa]c[ay]?\s*|^\s*w sprawie\s*", "", tail)
        przedmiot = tail.strip(" .")
        # ogranicz do pierwszego zdania jeśli długie
        if len(przedmiot) > 3 and "." in przedmiot:
            przedmiot = przedmiot.split(".")[0].strip()
    if not przedmiot:
        m_p = re.search(r"(?:dotycząca|w sprawie|dotyczący)\s+(.+?)(?:\.\s|\.$)", body)
        if m_p:
            przedmiot = m_p.group(1).strip()

    # PDF links
    tresc_url, odpowiedz_url = "", ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = _clean(a.get_text(" ", strip=True)).lower()
        if "/fobjects/download/" not in href:
            continue
        if re.search(r"odpowiedz|odpowiedź", txt) or re.search(r"odpowiedz", href, re.I):
            odpowiedz_url = href if href.startswith("http") else BASE + href
        elif re.search(r"interpelacj|zapytani|wniosk|tresc|treść", txt) or re.search(r"download", href, re.I):
            if not tresc_url:
                tresc_url = href if href.startswith("http") else BASE + href

    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    return {
        "typ": typ,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "klub": klub,
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "bip_url": url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Dębica (BIP gov.pl)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Dębicy ===")
    detail_urls = list_detail_urls(session, args.max_pages)
    print(f"  unikalnych linków detalów: {len(detail_urls)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    records = []
    for i, u in enumerate(detail_urls, 1):
        html = fetch_text(session, u)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        rec = parse_detail(soup, u)
        if not rec:
            _log(f"  [skip] {u}")
            continue
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        rec["cri"] = f"cri-debica-{i}"
        records.append(rec)
        time.sleep(DELAY)

    # dedupe
    seen = set()
    uniq = []
    for r in records:
        key = (r["bip_url"], r["przedmiot"], r["data_wplywu"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    records = uniq

    records.sort(key=lambda r: (r["rok"], r["data_wplywu"]), reverse=True)
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
