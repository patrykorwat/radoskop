#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Scraper interpelacji i zapytań radnych Rady Miasta Rybnika.

Źródło: BIP Miasta Rybnika (bip.um.rybnik.eu) — "Rada Miasta -> Interpelacje i
zapytania radnych" (Default.aspx?Page=352). Rejestr jest renderowany
serwerowo (jedna tabela z całą historią): kolumny
    Radny | Klub radnych | Data wpływu | Dotyczy | Data odpowiedzi | Szczegóły

Parsujemy wiersze tabeli, filtrujemy do bieżącej (IX) kadencji 2024-2029 i
zapisujemy w formacie Radoskop (ten sam schemat co bartoszyce/zary).

Uwaga: rejestr nie rozróżnia w tabeli typu interpelacji od zapytania — ustawiamy
typ="interpelacja" (kompromis rejestru bez szczegółowej strony tematu).

Użycie (wywoływane przez nas_city.sh):
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--cache-dir ...]
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
CONFIG = json.loads((HERE.parent.parent / "config.json").read_text(encoding="utf-8"))
CLUB_ASSIGN = CONFIG.get("club_assignments", {}) or {}
CLUB_FULL = CONFIG.get("clubs", {}) or {}

BASE = "https://bip.um.rybnik.eu"
LIST_URL = f"{BASE}/Default.aspx?Page=352"
IX_START = "2024-05-07"
KADENCJA = "2024-2029"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def reverse_name(nazwisko_imie: str) -> str:
    """'Brzózka Joanna' -> 'Joanna Brzózka' (kolejność Radoskop/imienna)."""
    p = nazwisko_imie.split()
    return f"{p[1]} {p[0]}" if len(p) == 2 else nazwisko_imie


def map_club(radny: str) -> str:
    code = CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    cl = CLUB_FULL.get(code, {})
    return cl.get("name", code)


def fetch(cache_dir: Path | None):
    import hashlib
    if cache_dir is not None:
        key = hashlib.md5(LIST_URL.encode()).hexdigest()
        cf = cache_dir / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    time.sleep(0.4)
    r = requests.get(LIST_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    if cache_dir is not None:
        cf = cache_dir / (key + ".html")
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(r.text, encoding="utf-8")
    return r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    html = fetch(Path(args.cache_dir) if args.cache_dir else None)
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', html, re.S):
        tds = [re.sub(r'<[^>]+>', '', t).strip() for t in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if len(tds) < 6 or not tds[0] or not re.match(r'\d{4}-\d\d-\d\d', tds[2]):
            continue
        radny, klub, data_wplywu, dotyczy, data_odpowiedzi = tds[0], tds[1], tds[2], tds[3], tds[4]
        if data_wplywu < IX_START:
            continue  # tylko bieżąca kadencja
        det = re.findall(r'href="[^"]*Id=(\d+)"', tr)
        rid = det[0] if det else ""
        radny_disp = reverse_name(radny)
        rok = int(data_wplywu[:4])
        odp_status = "Udzielono" if data_odpowiedzi else "Brak odpowiedzi"
        out.append({
            "cri": f"cri-rybnik-{rid}",
            "typ": "interpelacja",
            "rok": rok,
            "kadencja": KADENCJA,
            "radny": radny_disp,
            "przedmiot": dotyczy,
            "data_wplywu": data_wplywu,
            "klub": map_club(radny_disp),
            "odpowiedz_status": odp_status,
            "tresc_url": "",
            "odpowiedz_url": "",
            "data_odpowiedzi": data_odpowiedzi,
            "bip_url": f"{BASE}/Default.aspx?Page=352&Id={rid}" if rid else "",
        })

    out.sort(key=lambda x: x["data_wplywu"], reverse=True)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Interpelacje IX kad.: {len(out)}")


if __name__ == "__main__":
    main()
