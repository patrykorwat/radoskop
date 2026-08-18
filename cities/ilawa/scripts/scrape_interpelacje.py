#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Iławie.

Źródło: BIP Iławy (https://bip.umilawa.pl) — rejestr
"Interpelacje i zapytania radnych":
    https://bip.umilawa.pl/interpelacja/179/status/            (status)
    https://bip.umilawa.pl/interpelacja/179/{strona}/status/   (paginacja)
    https://bip.umilawa.pl/interpelacja/179/{id}/              (detale)

eSesja (https://ilawa.esesja.pl/interpelacje_i_zapytania) — moduł NIEAKTYWNY
("Brak aktywności lub moduł nieaktywny"), dlatego źródłem jest rejestr BIP.

Detal /interpelacja/179/{id}/:
    Status: udzielono odpowiedzi | (oczekuje)
    Radny: {Imię Nazwisko}
    Data złożenia: RRRR-MM-DD
    Tytuł (h1): "Interpelacja w sprawie ..." | "Zapytanie w sprawie ..."
    Załączniki: "Pobierz treść interpelacji/zapytania" (treść),
                "Odpowiedź na interpelację/zapytanie" (odpowiedź)

Klub radnego z config.json (club_assignments -> clubs).
BIP serwuje certyfikat bez zaufanego CA — requests z verify=False.

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
from html import unescape as _unescape
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.umilawa.pl"
REGISTER = f"{BASE}/interpelacja/179/status/"
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029
_VERIFY_TLS = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.35
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


# Detal: link /interpelacja/179/{id}/ (bez trailing /status/)
_DETAIL_RE = re.compile(r'href="([^"]*interpelacja/179/(\d+)/[^"]*)"')


def parse_listing(html):
    """Zwraca (detail_ids_set, next_page_number_or_None)."""
    ids = set()
    for m in _DETAIL_RE.finditer(html):
        href = m.group(1)
        if "status" in href:
            continue  # to link paginacji, nie detal
        ids.add(int(m.group(2)))
    # numeracja stron: /interpelacja/179/{n}/status/  (n>=2)
    pages = [int(p) for p in re.findall(r"/interpelacja/179/(\d+)/status/", html)]
    nxt = max(pages) if pages else None
    return ids, nxt


def parse_detail(html, url):
    def field(kw):
        i = html.find(kw)
        if i < 0:
            return ""
        seg = _clean(html[i:i + 400])
        seg = re.split(r"\s+(?:Data złożenia|Ogłoszono dnia|Status|Radny|Okres|Opis|Załączniki):", seg)[0]
        return seg[len(kw):].strip()

    # typ z nagłówka <h2>Interpelacja</h2>/<h2>Zapytanie</h2>/<h2>Wniosek</h2>
    typ = "interpelacja"
    m = re.search(r"<h2>(Interpelacja|Zapytanie|Wniosek)</h2>", html)
    if m:
        h = m.group(1).lower()
        if h == "zapytanie":
            typ = "zapytanie"
        elif h == "wniosek":
            typ = "wniosek"

    # przedmiot z pola "Opis:" (pełna treść, do "Załączniki")
    przedmiot = ""
    i = html.find("Opis:")
    if i >= 0:
        seg = _clean(html[i + 5:i + 600])
        przedmiot = re.split(r"\s+Załączniki\b", seg)[0].strip()
    if not przedmiot:
        # fallback: linia po "Szczegóły interpelacji / zapytania"
        m2 = re.search(r"Szczegóły interpelacji / zapytania\s*(.{10,200}?)\s*(?:Okres:|Status:)", html, re.S)
        if m2:
            przedmiot = _clean(m2.group(1))
    przedmiot = _unescape(przedmiot)

    radny = _unescape(field("Radny:"))
    data_wplywu = field("Data złożenia:")
    status_raw = field("Status:").lower()

    # Załączniki
    tresc_url, odpowiedz_url = "", ""
    for a in re.findall(r'<a[^>]+href="([^"]*pobierz\.php[^"]*)"[^>]*>(.*?)</a>', html, re.S):
        label = _clean(a[1]).lower()
        href = a[0].replace("&amp;", "&")
        if not href.startswith("http"):
            href = BASE + href
        if "odpowied" in label:
            if not odpowiedz_url:
                odpowiedz_url = href
        else:
            if not tresc_url:
                tresc_url = href

    rok = int(data_wplywu[:4]) if data_wplywu and data_wplywu[:4].isdigit() else MIN_ROK_DEFAULT
    answered = "udzielono" in status_raw or bool(odpowiedz_url)

    return {
        "typ": typ,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "odpowiedz_status": "Udzielono" if answered else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Iława (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = wszystkie")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Iława (BIP bip.umilawa.pl) ===")
    ids = set()
    n_pages = 0
    maxp = 1
    page_url = REGISTER
    while page_url:
        n_pages += 1
        html = fetch_text(session, page_url)
        if not html:
            print(f"  [skip] brak treści: {page_url}")
            break
        page_ids, nxt = parse_listing(html)
        before = len(ids)
        ids |= page_ids
        if nxt:
            maxp = max(maxp, nxt)
        print(f"  strona {n_pages}: {len(page_ids)} rekordów (łącznie {len(ids)})")
        if args.max_pages and n_pages >= args.max_pages:
            break
        if n_pages >= maxp:
            break
        page_url = f"{BASE}/interpelacja/179/{n_pages + 1}/status/"
    ord_ids = sorted(ids)
    print(f"  unikalnych rekordów: {len(ord_ids)}")

    records = []
    for i, rid in enumerate(ord_ids, 1):
        url = f"{BASE}/interpelacja/179/{rid}/"
        dhtml = fetch_text(session, url)
        if not dhtml:
            print(f"  [skip] brak treści {url}")
            continue
        d = parse_detail(dhtml, url)
        if not d or d["rok"] < MIN_ROK_DEFAULT:
            continue
        records.append({
            "cri": str(rid),
            "typ": d["typ"],
            "rok": d["rok"],
            "kadencja": "2024-2029" if d["rok"] >= 2024 else "2018-2024",
            "radny": d["radny"],
            "przedmiot": d["przedmiot"],
            "data_wplywu": d["data_wplywu"],
            "klub": _club_for(d["radny"]),
            "odpowiedz_status": d["odpowiedz_status"],
            "tresc_url": d["tresc_url"],
            "odpowiedz_url": d["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": url,
        })
        if i % 25 == 0:
            print(f"  ... {i}/{len(ord_ids)}")

    # bez odpowiedzi -> odpowiedz_status = "Nie udzielono" wg odpowiedz_url
    for r in records:
        if not r["odpowiedz_url"]:
            r["odpowiedz_status"] = "Nie udzielono"

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
