#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Kobyłka.

Źródło: BIP bipgmina.pl (https://kobylka.bipgmina.pl) — rejestr
"Interpelacje i zapytania radnych":
    Interpelacje: /wiadomosci/12016 (rok: /lista/{page}/{year}/...)
    Zapytania:    /wiadomosci/12017
    Pobierz z XML: {detail_url}/xml -> <tytul> + <zalaczniki><zalacznik><plik>

eSesja (https://kobylka.esesja.pl) — nie sprawdzany (rejestr BIP kompletny).

Klub radnego z config.json (club_assignments -> clubs).
Rejestr nie eksponuje statusu odpowiedzi dla bieżącej kadencji: odpowiedz_status
ustawiamy na "Udzielono" tylko gdy tytuł jawnie podaje "wraz z odpowiedzią";
w przeciwnym razie zostawiamy puste (uczciwie — źródło tego nie podaje).

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--debug]
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from html import unescape as _unescape
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://kobylka.bipgmina.pl"
CATS = [("interpelacja", 12016), ("zapytanie", 12017)]
YEARS = [2026, 2025, 2024]  # bieżąca kadencja 2024-2029
MIN_ROK = 2024
KAD_START = "2024-05-07"  # początek IX kadencji 2024-2029

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


def _best_radny(name):
    """Dopasuj nazwisko (mianownik) do config club_assignments po
    podobieństwie (genitiv w tytule -> mianownik w config)."""
    best, best_r = "", 0.0
    for cand in _CLUB_ASSIGN:
        r = SequenceMatcher(None, cand.lower(), name.lower()).ratio()
        if r > best_r:
            best, best_r = cand, r
    return best if best_r >= 0.8 else ""


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
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
    return re.sub(r"\s+", " ", s or "").strip()


def list_detail_ids(session, cat_id, year):
    """Zbierz ID-e artykułów dla roku (paginacja lista/{page}/{year})."""
    ids = []
    page = 1
    while True:
        u = f"{BASE}/wiadomosci/{cat_id}/lista/{page}/{year}"
        html = fetch_text(session, u)
        if not html:
            break
        found = [
            m
            for m in re.findall(
                rf"wiadomosci/{cat_id}/wiadomosc/(\d+)", html
            )
        ]
        ids.extend(found)
        # czy jest strona dalej w tym roku?
        pages = {
            int(p)
            for p in re.findall(rf"wiadomosci/{cat_id}/lista/(\d+)/{year}", html)
        }
        if not pages or page >= max(pages):
            break
        page += 1
    return list(dict.fromkeys(ids))


def parse_detail_xml(xml, url):
    """Z <tytul> i <zalaczniki><zalacznik><plik>."""
    tytul = ""
    m = re.search(r"<tytul>(.*?)</tytul>", xml, re.S)
    if m:
        tytul = _clean(_unescape(m.group(1)))
    files = re.findall(r"<plik>(.*?)</plik>", xml, re.S)
    tresc_url = _clean(files[0]) if files else ""

    typ = "interpelacja"
    tl = tytul.lower()
    if tl.startswith("zapytanie") or "zapytanie radn" in tl:
        typ = "zapytanie"

    # radny: "... Radnej {Imię Nazwisko} z dnia ..." / "... Radnego {Imię Nazwisko} ..."
    radny = ""
    m = re.search(r"\bRadny\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\d]+?)\s+z dnia\b", tytul, re.I)
    if not m:
        m = re.search(r"\bRadnej\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\d]+?)\s+z dnia\b", tytul, re.I)
    if not m:
        # variant: "... z dnia 25 marca 2024 r."
        m = re.search(
            r"\bRadnego\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\d]+?)\s+z dnia\b|\bRadnej\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\d]+?)\s+z dnia\b",
            tytul,
            re.I,
        )
    if m:
        raw = _clean(m.group(1) or m.group(2) or "").rstrip(".,")
        radny = _best_radny(raw) or raw
    if not radny and ("Radn" in tytul):
        radny = tytul  # fallback

    # data: "z dnia {D.MM.YYYY} r." | "{D MMMM YYYY} r."
    data_wplywu = ""
    m = re.search(r"z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", tytul)
    if m:
        d, mo, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        data_wplywu = f"{y}-{mo}-{d}"
    if not data_wplywu:
        m = re.search(r"z dnia\s+(\d{1,2})\s+([a-ząąćęłńóśźż]+)\s+(\d{4})", tytul, re.I)
        mies = {
            "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
            "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
            "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
        }
        if m:
            mo = mies.get(m.group(2).lower(), "")
            if mo:
                data_wplywu = f"{m.group(3)}-{mo}-{m.group(1).zfill(2)}"

    rok = int(data_wplywu[:4]) if data_wplywu[:4].isdigit() else 0

    # odpowiedź — tylko gdy tytuł jawnie o niej mówi
    answered = "odpowiedzi" in tl or "odpowiedzią" in tl
    odpowiedz_status = "Udzielono" if answered else ""

    przedmiot = tytul
    return {
        "typ": typ,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Kobyłka (BIP bipgmina)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Kobyłka (BIP bipgmina) ===")
    records = []
    seen = set()
    for typ, cat_id in CATS:
        for year in YEARS:
            ids = list_detail_ids(session, cat_id, year)
            print(f"  {typ} {year}: {len(ids)} pozycji w listingu")
            for rid in ids:
                if rid in seen:
                    continue
                seen.add(rid)
                u = f"{BASE}/wiadomosci/{cat_id}/wiadomosc/{rid}"
                xml = fetch_text(session, u + "/xml")
                if not xml:
                    print(f"  [skip] brak XML {u}")
                    continue
                d = parse_detail_xml(xml, u)
                if d["rok"] and d["rok"] < MIN_ROK:
                    continue
                if d["data_wplywu"] and d["data_wplywu"] < KAD_START:
                    continue  # rekord sprzed IX kadencji
                records.append({
                    "cri": rid,
                    "typ": d["typ"],
                    "rok": d["rok"],
                    "kadencja": "2024-2029" if d["rok"] >= 2024 else "",
                    "radny": d["radny"],
                    "przedmiot": d["przedmiot"],
                    "data_wplywu": d["data_wplywu"],
                    "klub": _club_for(d["radny"]),
                    "odpowiedz_status": d["odpowiedz_status"],
                    "tresc_url": d["tresc_url"],
                    "odpowiedz_url": "",
                    "data_odpowiedzi": "",
                    "bip_url": u,
                })

    # dedupe po cri
    uniq = {}
    for r in records:
        key = r["cri"] or r["bip_url"]
        if key not in uniq:
            uniq[key] = r
    records = list(uniq.values())
    records.sort(key=lambda r: r["rok"] or 0, reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    noyear = sum(1 for r in records if r["rok"] == 0)
    noklub = sum(1 for r in records if not r["klub"] and r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Razem: {len(records)}")
    print(f"bez roku: {noyear} | bez klubu: {noklub}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
