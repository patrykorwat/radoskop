#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Scraper interpelacji i zapytań radnych Rady Miejskiej Turku (AlfaTV "System Rada").

Źródło: https://rada.sobotka.pl/interpelacje — serwer-renderowana tabela
    Lp. | Temat | Rodzaj (interpelacja/zapytanie) | Interpelujący | Data wpływu | Data odpowiedzi
Cały rejestr jest na jednej stronie (klient-side DataTables dzieli ją tylko
wizualnie, wszystkie wiersze są w DOM). Rodzaj + radny + daty w kolumnach.

Filtrujemy do IX kadencji (data_wplywu >= 2024-05-07) i zapisujemy w formacie
Radoskop (ten sam schemat co bartoszyce/zary/rybnik).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--cache-dir ...]
"""

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
CONFIG = json.loads((HERE.parent.parent / "config.json").read_text(encoding="utf-8"))
CLUB_ASSIGN = CONFIG.get("club_assignments", {}) or {}
CLUB_FULL = CONFIG.get("clubs", {}) or {}

BASE = "https://rada.sobotka.pl"
LIST_URL = f"{BASE}/interpelacje"
IX_START = "2024-05-07"
KADENCJA = "2024-2029"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
DELAY = 0.4


def map_club(radny: str) -> str:
    code = CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    cl = CLUB_FULL.get(code, {})
    return cl.get("name", code)


def clean(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s+", " ", (s or "").replace("\xa0", " "))).strip()


def fetch(cache_dir: Path | None) -> str:
    if cache_dir is not None:
        key = hashlib.md5(LIST_URL.encode()).hexdigest()
        cf = Path(cache_dir) / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    txt = resp.text
    if cache_dir is not None:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        (Path(cache_dir) / (hashlib.md5(LIST_URL.encode()).hexdigest() + ".html")).write_text(
            txt, encoding="utf-8", errors="ignore")
    return txt


def parse(html: str):
    soup = BeautifulSoup(html, "html.parser")
    tbl = soup.find("table")
    if not tbl:
        return []
    records = []
    for tr in tbl.find_all("tr"):
        tds = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        tds = [t for t in tds if t]
        if len(tds) < 6:
            continue
        lp, temat, rodzaj, radny, data_wplywu, data_odp = tds[0], tds[1], tds[2], tds[3], tds[4], tds[5]
        if not re.match(r"^\d+$", lp):
            continue
        data_wplywu = re.sub(r"^\s*|\s*$", "", data_wplywu)
        if data_wplywu < IX_START:
            continue
        typ = "interpelacja" if "interpelacj" in rodzaj.lower() else "zapytanie"
        rok = int(data_wplywu[:4])
        klub = map_club(radny) if radny else ""
        records.append({
            "cri": f"cri-sobotka-{lp}",
            "typ": typ,
            "rok": rok,
            "kadencja": KADENCJA,
            "radny": radny,
            "przedmiot": temat,
            "data_wplywu": data_wplywu,
            "klub": klub,
            "odpowiedz_status": "Udzielono" if data_odp else "Nie udzielono",
            "tresc_url": "",
            "odpowiedz_url": "",
            "data_odpowiedzi": data_odp if data_odp else "",
            "bip_url": LIST_URL,
        })
    return records


def main():
    ap = argparse.ArgumentParser(description="Scraper interpelacji — Sobótka (AlfaTV)")
    ap.add_argument("--output", default="docs/interpelacje.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    html = fetch(Path(args.cache_dir) if args.cache_dir else None)
    records = parse(html)
    records.sort(key=lambda r: r["data_wplywu"], reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print(f"=== Interpelacje — Sobótka (AlfaTV) ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out}")


if __name__ == "__main__":
    main()
