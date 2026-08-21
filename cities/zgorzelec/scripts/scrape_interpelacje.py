#!/usr/bin/env python3
"""Radoskop Zgorzelec — interpelacje/zapytania z BIP (bip.info.pl).

Źródło: https://zgorzelec.bip.info.pl/idmp=769 (Kadencja 2024-2029).
Uwaga: config.json bip_url (bip.zgorzelec.pl) jest MARTWY (DNS-fail). Prawdziwy BIP
miasta = zgorzelec.bip.info.pl (link z oficjalnej strony zgorzelec.eu). eSesja
zgorzelec.esesja.pl ma moduł interpelacje NIEAKTYWNY.

Listing: tabela [Data publikacji | Symbol | Tytuł dokumentu], paginacja &istr=N.
Tytuł: "Interpelacja|Zapytanie radnej/radnego {Imię Nazwisko} ws./dot./w sprawie {przedmiot}".
Typ + radny (dopełniacz) + przedmiot z tytułu; radny fuzzy->config (mianownik).
Autorzy zbiorowi ("... radnej X i radnego Y") -> radny="", klub="".
Detal dokument.php?iddok={id}&idmp=769 : załączniki:
  - "interpelacja/zapytanie ...pdf"          -> tresc_url
  - "odp. na interpelację ...pdf" / "odpowiedź" -> odpowiedz_url  (jeśli jest)
data_wplywu = Data publikacji (jedyna data oficjalna w źródle; brak daty złożenia).
Użycie: python3 scrape_interpelacje.py --output docs/interpelacje.json
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

BASE = "https://zgorzelec.bip.info.pl"
LIST_URL = f"{BASE}/index.php?idmp=769&r=r"
DETAIL_MP = 769
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False


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


def _abspath(u: str) -> str:
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return BASE + u
    return BASE + "/" + u


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


# "Interpelacja radnej Marzenny Bartczak ws. ..." / "Zapytanie radnego Tomasza Śnieżyka dot. ..."
_TITLE_RE = re.compile(
    r"^(?P<typ>Interpelacja|Zapytanie)\s+(?P<rest>.+)$", re.I)


def parse_title(title):
    m = _TITLE_RE.match(title.strip())
    if not m:
        return "interpelacja", "", ""
    typ = "interpelacja" if m.group("typ").lower().startswith("interpelacj") else "zapytanie"
    rest = m.group("rest")
    # nazwisko do 'ws.'/'w sprawie'/'dot.'
    nm = re.match(
        r"^(?:radnej|radnego|radny|radni|radne)\s+(?P<names>.+?)\s+"
        r"(?:ws\.?\s*|w\s+sprawie\s+|dot\.?\s*)(?P<subj>.+)$", rest, re.I)
    if not nm:
        return typ, "", ""
    names = re.sub(r"\s+", " ", nm.group("names")).strip()
    subj = re.sub(r"\s+", " ", nm.group("subj")).strip()
    subj = re.sub(r"[。.]\s*$", "", subj).strip()
    # autorzy zbiorowi
    collective = (" i " in names) or (", " in names) or ("oraz" in names.lower())
    if collective:
        return typ, "", subj
    matched = _match_nominative(names)
    # pełne nazwisko z tytułu jest wiarygodne nawet gdy brak go w config (klub pusto)
    radny = matched if matched else names
    return typ, radny, subj


def parse_listing_page(html):
    """Zwraca listę (data, title, href)."""
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            data_cell = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).strip()
            dm = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", data_cell)
            if not dm:
                continue
            data = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
            a = cells[2].find("a", href=True)
            if not a:
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            href = _abspath(a["href"])
            out.append({"data": data, "title": title, "href": href})
        if out:
            return out
    return out


def has_next_page(html):
    for a in re.finditer(r'href="([^"]*istr=\d+[^"]*)"[^>]*>\s*następna', html):
        return True
    # also common: "następna" link text
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower().startswith("następna") and "istr" in a["href"]:
            return True
    return False


def parse_detail(html):
    """Zwraca (tresc_url, odpowiedz_url)."""
    tresc_url, odpowiedz_url = "", ""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find(id="content-main") or soup
    # attachments: links plik.php?id=...
    # pierшszy załącznik zawierający 'interpelacj'/'zapytani' = tresc; 'odp.' = odpowiedz
    for a in content.find_all("a", href=True):
        href = a["href"]
        if "plik.php" not in href and ".pdf" not in href.lower():
            continue
        label = a.get_text(" ", strip=True).lower()
        if not label:
            continue
        href = _abspath(a["href"])
        if ("odp." in label or "odpowied" in label or "odpowiedz" in label):
            if not odpowiedz_url:
                odpowiedz_url = href
        elif "interpelacj" in label or "zapytani" in label:
            if not tresc_url:
                tresc_url = href
    return tresc_url, odpowiedz_url


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Zgorzelec (bip.info.pl)")
    parser.add_argument("--output", default="cities/zgorzelec/docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Zgorzelec (bip.info.pl idmp=769) ===")
    # page count via istr
    items = []
    page = 1
    while True:
        url = LIST_URL if page == 1 else f"{LIST_URL}&istr={page}"
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] strona {page} brak treści")
            break
        found = parse_listing_page(html)
        items.extend(found)
        if args.max_pages and page >= args.max_pages:
            break
        if not has_next_page(html):
            break
        page += 1
        time.sleep(DELAY)
    print(f"  stron: {page} | pozycji na listingach: {len(items)}")

    seen_u, uniq = set(), []
    for it in items:
        if it["href"] in seen_u:
            continue
        seen_u.add(it["href"])
        uniq.append(it)
    items = uniq

    min_rok = None if args.all else 2024
    records = []
    for it in items:
        rok = int(it["data"][:4])
        if min_rok and rok < min_rok:
            continue
        typ, radny, subj = parse_title(it["title"])
        idm = re.search(r"iddok=(\d+)", it["href"])
        cri = f"cri-zgorzelec-{idm.group(1)}" if idm else f"cri-zgorzelec-{len(records)}"
        det_html = fetch_text(session, it["href"])
        tresc_url, odpowiedz_url = parse_detail(det_html)
        records.append({
            "cri": cri,
            "typ": typ,
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
            "radny": radny,
            "przedmiot": subj or it["title"],
            "data_wplywu": it["data"],
            "klub": _club_for(radny) if radny else "",
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": "",
            "bip_url": it["href"],
        })
        time.sleep(DELAY)

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in records if not r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | "
          f"Bez radnego: {no_radny} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
