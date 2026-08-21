#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Lubonia (BIP 2ClickPortal).

Źródło: BIP UM Luboń (2ClickPortal) — strona "Interpelacje radnych" z rocznymi
podstronami (bieżąca kadencja 2024-2029):

    https://bip.lubon.pl/2024-interpelacje.html
    https://bip.lubon.pl/2025-interpelacje.html
    https://bip.lubon.pl/2026-interpelacje.html

Struktura każdej podstrony rocznej: jedna tabela HTML o kolumnach
    Lp. | Składający | Dane | Przedmiot | Odpowiedź
Każdy wiersz danych = jeden rekord:
  - Składający -> radny (pełne imię i nazwisko)
  - Dane: data wpływu (DD.MM.RRRR)
  - Przedmiot: link do PDF interpelacji (tresc_url)
  - Odpowiedź: data odpowiedzi + link do PDF odpowiedzi (lub puste)
Typ: w Luboniu źródło publikuje wyłącznie interpelacje (brak zapytań w tabeli
rocznej) -> domyślnie "interpelacja".
Przedmiot: pole "Przedmiot" zawiera opis/skrót przez "dotyczy : ..."; pełna treść
w PDF-załączniku. Używamy czystego tekstu z kolumny Przedmiot (NIE PDF, który
bywa skanem) — to uczciwy przedmiot z metadanych źródła.
Odpowiedź: status "Udzielono" gdy jest data/link odpowiedzi, data_odpowiedzi z
kolumny. Klub radnego z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/x
"""

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.lubon.pl/"
INDEX_URL = BASE + "interpelacje.html"
YEARS = [2024, 2025, 2026]  # bieżąca kadencja 2024-2029 (podstrony roczne istnieją)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.7


def _load_clubs() -> tuple[dict, dict]:
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def fetch_text(session, url) -> str:
    for attempt in range(4):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                print(f"  [retry {attempt+1}] {resp.status_code} {url}")
                time.sleep(5)
                continue
        except requests.RequestException as e:
            print(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _normalize_date(s: str) -> str:
    """DD.MM.RRRR albo D.M.RRRR -> RRRR-MM-DD."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", s or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_year_page(html: str, rok: int, page_url: str) -> list[dict]:
    out = []
    tables = re.findall(r"<table.*?</table>", html, re.S | re.I)
    if not tables:
        return out
    for tb in tables:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S | re.I)
        for row in rows:
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            if len(tds) < 5:
                continue
            lp, radny, dane, przedmiot_td, odp_td = tds[:5]
            lp_txt = _clean(lp)
            if lp_txt in ("", "Lp.", "\xa0", "Lp"):
                continue
            radny_txt = _clean(radny)
            if not radny_txt:
                continue
            dane_txt = _clean(dane)  # może zawierać sztuczny "0 " na początku
            dane_txt = re.sub(r"^\s*0\s+", "", dane_txt)
            data_iso = _normalize_date(dane_txt)
            rok_rec = int(data_iso[:4]) if len(data_iso) >= 4 and data_iso[:4].isdigit() else rok

            przedmiot = _clean(przedmiot_td)
            # linki: pierwszy PDF/mniejszy w Przedmiot (tresc_url)
            tresc_hrefs = [urljoin(BASE, h) for h in re.findall(r'href="([^"]+)"', przedmiot_td)]
            tresc_url = tresc_hrefs[0] if tresc_hrefs else ""

            odp_txt = _clean(odp_td)
            odp_hrefs = [urljoin(BASE, h) for h in re.findall(r'href="([^"]+)"', odp_td)]
            odpowiedz_url = odp_hrefs[0] if odp_hrefs else ""
            # data odpowiedzi w kolumnie odpowiedzi (pierwsza DD.MM.RRRR)
            odp_date = _normalize_date(odp_txt)
            odpowiedz_status = "Udzielono" if (odp_hrefs or odp_date) else "Nie udzielono"

            cri = f"lubon-{rok_rec}-{lp_txt.rstrip('.')}".strip()

            out.append({
                "cri": cri,
                "typ": "interpelacja",  # źródło publikuje tylko interpelacje
                "rok": rok_rec,
                "kadencja": "2024-2029" if rok_rec >= 2024 else ("2018-2024" if rok_rec else ""),
                "radny": radny_txt,
                "przedmiot": przedmiot,
                "data_wplywu": data_iso,
                "klub": _club_for(radny_txt),
                "odpowiedz_status": odpowiedz_status,
                "tresc_url": tresc_url,
                "odpowiedz_url": odpowiedz_url,
                "data_odpowiedzi": odp_date,
                "bip_url": page_url,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Luboń (BIP 2ClickPortal)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args()

    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Luboń (BIP 2ClickPortal) ===")
    records = []
    seen = set()
    for rok in YEARS:
        url = f"{BASE}{rok}-interpelacje.html"
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści {rok}: {url}")
            continue
        recs = parse_year_page(html, rok, url)
        print(f"  {rok}: {len(recs)} rekordów")
        for r in recs:
            if r["cri"] in seen:
                continue
            seen.add(r["cri"])
            records.append(r)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_club = sum(1 for r in records if not r["klub"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | z odpowiedzią: {answered} "
          f"| bez klubu: {no_club} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
