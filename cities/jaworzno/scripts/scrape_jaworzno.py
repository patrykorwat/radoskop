#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Jaworzno — imienne głosowania Rady Miejskiej w Jaworznie (IX kadencja 2024-2029).

Źródło: BIP bip.jaworzno.pl — platforma Madkom BIP v2 (Angular SPA, API /api/).
Kategoria "Imienne głosowania Radnych na Sesjach RM" = menu id 20227:
  GET /api/menu/20227/articles?limit=50&offset=0        -> lista artykułów "Zestawienie imiennych
       głosowań podczas <ROM> [nadzwyczajnej ]sesji ... w dniu <date> r."
  GET /api/articles/{id}                                 -> attachment id
  GET /api/pobierz/get.html?id={aid}                     -> PDF (wydruk eSesja, tekstowy,
       1 głos/strona, tabela dwukolumnowa Lp|Nazwisko i imię|Głos: ZA/PRZECIW/WSTRZYMUJE/
       OBECNY/NIEOBECNY) — ten sam format co miasta miedzyrzecz/goleniow.
Parser PDF: współdzielony moduł scraper Międzyrzecza (import przez ścieżkę repo).

Kluby: artykuł 45709 "Kluby Radnych ... kadencji 2024-2029" (oficjalny wykaz) —
club_assignments w config.json (kuratorowane ręcznie, weryfikacja 2026-09-01).

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python scrape_jaworzno.py --city-dir <dir> [--cache-dir d] [--skip-download]
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent.parent.parent     # scripts -> jaworzno -> cities -> radoskop
sys.path.insert(0, str(REPO / "cities" / "miedzyrzecz" / "scripts"))
import scrape_miedzyrzecz as _print_parser  # noqa: E402  (wydruk eSesja parser + build_*)

BIP = "https://bip.jaworzno.pl"
API = BIP + "/api"
MENU_VOTES = 20227          # "Imienne głosowania Radnych na Sesjach RM"
KAD_START = "2024-05-07"
KADENCJA_ID = _print_parser.KADENCJA_ID
KADENCJA_LABEL = _print_parser.KADENCJA_LABEL
REQ_DELAY = 0.6
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}


def _get(url, cache_dir=None, binary=False):
    global _LAST
    import hashlib
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + (".bin" if binary else ".dat"))
        if cf.is_file() and cf.stat().st_size > 0:
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
    r.raise_for_status()
    data = r.content
    if cf is not None:
        cf.write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


def _api_json(path, cache_dir=None):
    return json.loads(_get(API + path, cache_dir))


_ROMAN = re.compile(r"podczas\s+([IVXLCDM]+)(?:\s+nadzwyczajnej)?\s+sesji", re.I)


def discover_sessions(cache_dir=None):
    d = _api_json(f"/menu/{MENU_VOTES}/articles?limit=100&offset=0", cache_dir)
    out = []
    for a in d.get("articles", []):
        title = next((f["value"] for f in a.get("aliasFields", []) if f.get("alias") == "title"), "")
        dm = re.search(r"w dniu (\d{1,2}) (\w+) (\d{4})", title)
        rm = _ROMAN.search(title)
        if not dm or not rm or dm.group(2).lower() not in MONTHS:
            continue
        date = f"{dm.group(3)}-{MONTHS[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
        if date < KAD_START:
            continue
        num = rm.group(1).upper()
        out.append({"num": num, "date": date, "title": title, "aid": int(a["id"])})
    out.sort(key=lambda s: s["date"])
    return out


def attachment_pdf_id(article_id, cache_dir=None):
    a = _api_json(f"/articles/{article_id}", cache_dir)
    atts = a.get("attachments") or []
    for att in atts:
        if (att.get("extension") or "").lower() == "pdf":
            return att["id"]
    return None


def parse_pdf(data):
    recs = _print_parser.records_from_pdf(data)
    # wydruk Jaworzna zawiera wakat "Mandat Nieobsadzony" -> nie radny; usuń (ok flag liczony przed)
    for r in recs:
        for cat in list(r["named"].keys()):
            r["named"][cat] = [n for n in r["named"][cat] if "andat" not in n and "ieobsadzony" not in n]
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[jaworzno] {len(sessions)} sesji IX kad. ({sessions[0]['date']} .. {sessions[-1]['date']})")

    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(exist_ok=True)
    records = []
    vstat = {"v": 0, "ok": 0, "fail": 0}
    for se in sessions:
        pf = pdf_dir / f"{se['num']}_{se['date']}.pdf"
        if not (pf.is_file() and pf.stat().st_size > 1000):
            if args.skip_download:
                print(f"  [skip {se['date']} no pdf cached]")
                continue
            att = attachment_pdf_id(se["aid"], cache)
            if not att:
                print(f"  [skip {se['date']} no attachment]")
                continue
            data = _get(f"{API}/pobierz/get.html?id={att}", cache, binary=True)
            if data[:4] != b"%PDF":
                print(f"  [skip {se['date']} not pdf? {len(data)}b]")
                continue
            pf.write_bytes(data)
        recs = parse_pdf(pf.read_bytes())
        nok = sum(1 for r in recs if r["ok"])
        vstat["v"] += len(recs); vstat["ok"] += nok; vstat["fail"] += len(recs) - nok
        for r in recs:
            r["date"] = se["date"]; r["num"] = se["num"]
        records.extend(recs)
        print(f"  [{se['num']:>4} {se['date']}] votes={len(recs)} ok={nok}")

    records = [r for r in records if r["ok"]]
    print(f"[jaworzno] zwalidowane głosy {vstat['ok']}/{vstat['v']} (odrzucone {vstat['fail']})")

    out, tv, ts = _print_parser.build_output(records, club_assign)
    profiles = _print_parser.build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data_json = {
        "city_name": cfg.get("city_name", "Jaworzno"),
        "rada_name": cfg.get("rada_name", "Rada Miejska w Jaworznie"),
        "generated": out["generated"],
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "stats": {"total_votes": tv, "total_sessions": ts,
                  "total_councilors": out["kadencje"][0]["total_councilors"]},
    }
    (docs / "data.json").write_text(json.dumps(data_json, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[jaworzno] OK votes={tv} sessions={ts} councilors={data_json['stats']['total_councilors']}")


if __name__ == "__main__":
    main()
