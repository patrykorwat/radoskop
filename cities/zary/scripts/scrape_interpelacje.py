#!/usr/bin/env python3
"""Radoskop Żary (miasto) — interpelacje/zapytania z BIP (SYSTEMDOBIP, E-LINE).

Źródło: https://bip.zary.pl/802/Interpelacje_i_zapytania/  (paginacja /802/.../{n}/)
Uwaga: eSesja zary.esesja.pl należy do Rady GMINY Żary (inna jednostka); dla miasta
Żary źródłem jest BIP bip.zary.pl.

Listing: <div class="information"><p class="phx ph3">{tytuł}</p><div
    class="read-more-wrapper"><a class="read-more-1" href="/802/{id}/{slug}/">czytaj więcej</a>
Detal: tytuł w p.phx.ph3; załączniki w <ul class="attachments"><li><a href="...pobierz.php?plik={nazwa}.pdf">
    <span>{Interpelacja|Zapytanie|Odpowiedź} (PDF, ...)</span></a>
    <span>Data wytworzenia informacji: <em>YYYY-MM-DD</em></span></li>...
Typ = etykieta PIERWSZEGO załącznika (Interpelacja/Zapytanie); tresc = Math. załącznik PDF;
odpowiedź = załącznik 'Odpowiedź' (jeśli jest). data_wplywu/data_odpowiedzi = 'Data
wytworzenia informacji' odpowiednich załączników.

RADNY NIE występuje w metadanych BIP (tylko w skanie PDF) -> radny/klub puste
(source=partial, MISZGU — tak jak Żagań, nie zgadujemy). przedmiot = tytuł.

Użycie: python3 scrape_interpelacje.py --output docs/interpelacje.json
"""
import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.zary.pl"
LIST_URL = f"{BASE}/802/Interpelacje_i_zapytania/"
MIN_ROK_DEFAULT = 2024
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


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


def _last_page(html) -> int:
    best = 1
    for m in re.finditer(rf"{re.escape(LIST_URL)}(\d+)/", html):
        best = max(best, int(m.group(1)))
    return best


def parse_listing(html):
    """Zwraca listę (bip_url, title) z bloków div.information."""
    items = []
    for m in re.finditer(
            r'<p class="phx ph3">(?P<title>.*?)</p>.*?'
            r'<a[^>]+href="(?P<href>[^"]+)"[^>]*class="read-more-1"', html, re.S):
        title = re.sub(r"<[^>]+>", " ", m.group("title"))
        title = re.sub(r"\s+", " ", unescape(title)).strip()
        href = m.group("href")
        if not href.startswith("http"):
            href = BASE + href
        items.append({"bip_url": href, "title": title})
    return items


_ATT_ITEM_RE = re.compile(
    r'<li\b[^>]*>(?P<body>.*?)</li>', re.S)


def _att_meta(body):
    """Z li załącznika: (etykieta, pdf_url, data_wytworzenia)."""
    sm = re.search(r"<span>(?P<label>[^<]+)</span>", body)
    label = sm.group("label").strip() if sm else ""
    pm = re.search(r"pobierz\.php\?plik=([^&\"']+)", body)
    pdf = pm.group(1) if pm else ""
    if pdf:
        pdf = "https://bip.zary.pl/system/pobierz.php?plik=" + pdf.replace(" ", "%20")
    dm = re.search(r"Data wytworzenia informacji:\s*<em>(\d{4}-\d{2}-\d{2})</em>", body)
    data = dm.group(1) if dm else ""
    return label, pdf, data


def parse_detail(html):
    """Zwraca dict: typ, przedmiot, tresc_url, odpowiedz_url, data_wplywu, data_odp."""
    title = ""
    tm = re.search(r'<p class="phx ph3">(?P<t>.*?)</p>', html, re.S)
    if tm:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(tm.group("t")))).strip()
    typ, tresc_url, odpowiedz_url = "", "", ""
    data_wplywu, data_odp = "", ""
    # attachments li blocks
    m = re.search(r'<ul class="attachments">(?P<uls>.*?)</ul>', html, re.S)
    if m:
        lis = _ATT_ITEM_RE.findall(m.group("uls"))
        for li in lis:
            label, pdf, data = _att_meta(li)
            low = label.lower()
            if low.startswith("interpelacj") or low.startswith("zapytani"):
                if not typ:
                    typ = "interpelacja" if low.startswith("interpelacj") else "zapytanie"
                if not tresc_url:
                    tresc_url = pdf
                    data_wplywu = data
            elif low.startswith("odpowied"):
                if not odpowiedz_url:
                    odpowiedz_url = pdf
                    data_odp = data
    if not typ:
        typ = "interpelacja"
    return {
        "typ": typ, "przedmiot": title, "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url, "data_wplywu": data_wplywu, "data_odp": data_odp,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Żary (BIP SYSTEMDOBIP)")
    parser.add_argument("--output", default="cities/zary/docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Żary (BIP bip.zary.pl) ===")
    html = fetch_text(session, LIST_URL)
    total_pages = _last_page(html)
    pages = min(total_pages, args.max_pages) if args.max_pages else total_pages
    print(f"  stron listingu: {total_pages} (przetwarzam {pages})")

    items = parse_listing(html)
    for page in range(2, pages + 1):
        time.sleep(DELAY)
        ph = fetch_text(session, f"{LIST_URL}{page}/")
        if not ph:
            print(f"  [skip] strona {page} brak treści")
            continue
        items.extend(parse_listing(ph))

    # dedupe by url
    seen_u, uniq = set(), []
    for it in items:
        if it["bip_url"] in seen_u:
            continue
        seen_u.add(it["bip_url"])
        uniq.append(it)
    items = uniq
    print(f"  pozycji na listingach (po dedupe): {len(items)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    records = []
    for it in items:
        det = parse_detail(fetch_text(session, it["bip_url"]))
        # rok z data_wplywu
        rok = 0
        if det["data_wplywu"]:
            rok = int(det["data_wplywu"][:4])
        if min_rok and rok < min_rok:
            continue
        if rok == 0:
            continue
        idm = re.search(r"/(\d{2,})/[^/]+/?$", it["bip_url"])
        cri = f"cri-zary-{idm.group(1)}" if idm else f"cri-zary-{len(records)}"
        records.append({
            "cri": cri,
            "typ": det["typ"],
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
            "radny": "",
            "przedmiot": det["przedmiot"] or it.get("title", ""),
            "data_wplywu": det["data_wplywu"],
            "klub": "",
            "odpowiedz_status": "Udzielono" if det["odpowiedz_url"] else "Nie udzielono",
            "tresc_url": det["tresc_url"],
            "odpowiedz_url": det["odpowiedz_url"],
            "data_odpowiedzi": det["data_odp"],
            "bip_url": it["bip_url"],
        })
        time.sleep(DELAY)

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | "
          f"Radny w metadanych: brak (source=partial) | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
