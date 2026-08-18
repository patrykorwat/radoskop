#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Elblągu.

Źródło: BIP Elbląga (https://bip.elblag.eu) — rejestr "Interpelacje i zapytania".

    https://bip.elblag.eu/interpelacje/72            (rejestr, kadencja 2024-2029)
    https://bip.elblag.eu/interpelacje/{page}/10     (paginacja, 10 rekordów na stronę)

eSesja (https://elblag.esesja.pl/interpelacje_i_zapytania) — moduł NIEAKTYWNY
("Brak aktywności lub moduł nieaktywny"), dlatego źródłem jest rejestr na BIP.

Rejestr: każda strona listingu = tabela rekordów; każdy rekord ma wiersze
  "Interpelacja w sprawie" (link do detalu /interpelacja/{id}/{slug}, przedmiot)
  "Tożsamość radnego" (radna/radny {Imię Nazwisko}).

Detal /interpelacja/{id}/{slug} — tabela metryk:
  Typ wystąpienia   -> Interpelacja | Zapytanie | (rzadko Wniosek)
  Tożsamość radnego -> radna/radny {Imię Nazwisko}
  w sprawie         -> przedmiot
  Załączniki:
    "Interpelacja skan"  (treść) — Data wytworzenia <time datetime=...> = data wystąpienia
    "Odpowiedź ..."       (odpowiedź)

Klub radnego z config.json (club_assignments -> clubs, fuzzy do mianownika).

BIP serwuje certyfikat bez zaufanego CA — w requesy używamy verify=False
(wzorzec olsztyn/zabrze). SSL-warning tłumimy.

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--max-pages N]
"""

import argparse
import json
import re
import sys
import time
import urllib3
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.elblag.eu"
REGISTER = f"{BASE}/interpelacje/72"
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029
_VERIFY_TLS = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.4
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


def _club_for(radny):
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_nominative(parsed):
    """Dopasowuje 'radna Iwona Łuczak'/'Iwona Łuczak' do klucza config (mianownik)."""
    best, best_ratio = "", 0.0
    p = parsed.lower()
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, p, name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else parsed


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30, verify=_VERIFY_TLS)
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


def parse_listing(html):
    """Zwraca listę dictów z listingu: {detail_url, przedmiot, radny_raw}."""
    soup = BeautifulSoup(html, "html.parser")
    # Rejestr: każdy rekord to <table> z wierszami. Znajdujemy wszystkie tabele z linkiem /interpelacja/
    out = []
    for table in soup.find_all("table"):
        a = table.find("a", href=re.compile(r"/interpelacja/\d+/"))
        if not a:
            continue
        detail_url = a["href"]
        przedmiot = _clean(a.get_text(" ", strip=True))
        # Tożsamość radnego
        radny_raw = ""
        for th in table.find_all("th"):
            if "Tożsamość radnego" in _clean(th.get_text()):
                td = th.find_next_sibling("td")
                if td:
                    radny_raw = _clean(td.get_text(" ", strip=True))
                break
        out.append({
            "detail_url": detail_url if detail_url.startswith("http") else BASE + detail_url,
            "przedmiot": przedmiot,
            "radny_raw": radny_raw,
        })
    return out


def _th_td(soup):
    """Słownik {nagłówek_th: wartość_td} z tabeli detalowej."""
    m = {}
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and td:
            m[_clean(th.get_text(" ", strip=True))] = _clean(td.get_text(" ", strip=True))
    return m


def parse_detail(html, listing):
    soup = BeautifulSoup(html, "html.parser")
    md = _th_td(soup)

    typ_raw = md.get("Typ wystąpienia", "")
    low = typ_raw.lower()
    if low.startswith("zapytani"):
        typ = "zapytanie"
    elif low.startswith("wniosek"):
        typ = "wniosek"
    else:
        typ = "interpelacja"

    radny_raw = md.get("Tożsamość radnego", "") or listing.get("radny_raw", "")
    # usuń prefiks radna/radny
    radny_raw2 = re.sub(r"^\s*(radna|radny|pani|pan)\s+", "", radny_raw, flags=re.I)
    radny = _match_nominative(radny_raw2)

    przedmiot = md.get("w sprawie", "") or listing.get("przedmiot", "")

    # Załączniki: treść ("Interpelacja ...") + odpowiedź; data z metryki treści
    tresc_url, odpowiedz_url, data_wplywu = "", "", ""
    attach_blocks = soup.select("#attachments div.header") or soup.select("section.attachments div.header")
    for div in attach_blocks:
        a = div.find("a", href=True)
        if not a:
            continue
        caption = _clean(a.get_text(" ", strip=True)).lower()
        href = a["href"]
        full = href if href.startswith("http") else BASE + href
        if "odpowied" in caption:
            if not odpowiedz_url:
                odpowiedz_url = full
        elif "interpelac" in caption or "zapytan" in caption or "wniosek" in caption:
            if not tresc_url:
                tresc_url = full
                # data z metryki tego załącznika
                parent = a.find_parent("div", class_="header")
                t = parent.find_next_sibling() if parent else None
                if t and getattr(t, "get", None):
                    time_el = t.find("time", attrs={"datetime": True})
                    if time_el:
                        data_wplywu = time_el["datetime"][:10]

    if not tresc_url:
        # fallback: pierwszy nie-odpowiedziowy załącznik
        for div in attach_blocks:
            a = div.find("a", href=True)
            if not a:
                continue
            caption = _clean(a.get_text(" ", strip=True)).lower()
            if "odpowied" not in caption:
                full = a["href"] if a["href"].startswith("http") else BASE + a["href"]
                tresc_url = full
                parent = a.find_parent("div", class_="header")
                t = parent.find_next_sibling() if parent else None
                if t and getattr(t, "get", None):
                    time_el = t.find("time", attrs={"datetime": True})
                    if time_el:
                        data_wplywu = time_el["datetime"][:10]
                break

    rok = int(data_wplywu[:4]) if data_wplywu and data_wplywu[:4].isdigit() else MIN_ROK_DEFAULT
    return {
        "typ": typ,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Elbląg (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = wszystkie")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Elbląg (BIP bip.elblag.eu) ===")
    listings = []
    page_url = REGISTER
    n_pages = 0
    while page_url:
        n_pages += 1
        html = fetch_text(session, page_url)
        if not html:
            print(f"  [skip] brak treści: {page_url}")
            break
        recs = parse_listing(html)
        listings.extend(recs)
        # następna strona
        m_next = re.search(r'<a[^>]+href="([^"]*?interpelacje/(\d+)/10)"[^>]*>\s*następna', html)
        page_url = m_next.group(1) if m_next else None
        print(f"  strona {n_pages}: {len(recs)} rekordów")
        time.sleep(DELAY)
    print(f"  stron rejestru: {n_pages}")

    # dedupe po detail_url
    seen = set()
    uniq = []
    for r in listings:
        if r["detail_url"] in seen:
            continue
        seen.add(r["detail_url"])
        uniq.append(r)
    print(f"  unikalnych rekordów w rejestrze: {len(uniq)}")

    records = []
    for i, item in enumerate(uniq, 1):
        dhtml = fetch_text(session, item["detail_url"])
        if not dhtml:
            print(f"  [skip] brak treści detalu {item['detail_url']}")
            continue
        detail = parse_detail(dhtml, item)
        if detail["rok"] < MIN_ROK_DEFAULT:
            continue
        m_id = re.search(r"/interpelacja/(\d+)", item["detail_url"])
        records.append({
            "cri": m_id.group(1) if m_id else f"elblag-{i}",
            "typ": detail["typ"],
            "rok": detail["rok"],
            "kadencja": "2024-2029" if detail["rok"] >= 2024 else "2018-2024",
            "radny": detail["radny"],
            "przedmiot": detail["przedmiot"],
            "data_wplywu": detail["data_wplywu"],
            "klub": _club_for(detail["radny"]),
            "odpowiedz_status": detail["odpowiedz_status"],
            "tresc_url": detail["tresc_url"],
            "odpowiedz_url": detail["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": item["detail_url"],
        })
        if i % 25 == 0:
            print(f"  ... {i}/{len(uniq)}")
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
