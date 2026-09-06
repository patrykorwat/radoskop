#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Zielona Góra — imienne głosowania Rady Miasta Zielona Góra (IX kadencja 2024-2029).

Źródło: BIP bip.zielonagora.pl (Wrota Lubuskie / custom CMS). Rejestr "Uchwały i Zarządzenia"
= /akty/144/{page}/typ/ (paginacja ~20/str, chronologicznie malejąco). Artykuł uchwały
/akty/144/{id}/... zawiera załącznik "Wyniki głosowania (PDF)" -> /system/pobierz.php?plik=WG-...
= wydruk eSesja (tekstowy, 1 głos/PDF, tabela Lp|Nazwisko i imię|Głos) — ten sam format co
miedzyrzecz/goleniow/jaworzno; parser współdzielony (import scrape_miedzyrzecz).

Sesja: nagłówek PDF "<N> <ROM> sesja Rady Miasta Zielona Góra" + "Data głosowania: dd.mm.rrrr".
Kategoria listy mieści też zarządzenia prezydenta (nr typu 940.2026 bez numery rzymskiej) —
pominięte; uchwały rady mają nr <ROM>.<n>.<rok>.

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python scrape_zielona_gora.py --city-dir <dir> [--cache-dir d] [--skip-download]
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
REPO = HERE.parent.parent.parent.parent     # scripts -> {slug} -> cities -> radoskop
sys.path.insert(0, str(REPO / "cities" / "miedzyrzecz" / "scripts"))
import scrape_miedzyrzecz as _print_parser  # noqa: E402

BIP = "https://bip.zielonagora.pl"
AKTY_LIST = BIP + "/akty/144/{page}/typ/"
KAD_START = "2024-05-07"
KADENCJA_ID = _print_parser.KADENCJA_ID
KADENCJA_LABEL = _print_parser.KADENCJA_LABEL
REQ_DELAY = 0.4
_LAST = 0.0
_UCHVALA_NUM = re.compile(r"^([IVXLCDM]+)\.\d+\.\d{4}$")


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


def crawl_list(cache_dir=None):
    """Lista /akty/144 -> [{date,url,num,title}] uchwały IX kad. (chronologicznie rosnąco)."""
    items, seen = [], set()
    for page in range(1, 400):
        html = _get(AKTY_LIST.format(page=page), cache_dir)
        rows = re.findall(
            r'<td class="td-date-1"><div>Data podjęcia</div>([\d-]+)</td>.*?'
            r'<td class="td-title-2"><div>Tytuł aktu</div><a href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<td class="td-title-3"><div>Nr aktu prawnego</div>([^<]*)</td>', html, re.S)
        if not rows:
            break
        stop = False
        for date, href, title, num in rows:
            num = num.strip()
            if num in seen:
                continue
            seen.add(num)
            m = _UCHVALA_NUM.match(num)
            if date >= KAD_START and m:
                items.append({"date": date, "url": href.replace("&amp;", "&"), "num": num,
                              "sesja": m.group(1),
                              "title": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip()})
            if date < KAD_START:
                stop = True
        if stop:
            break
    items.sort(key=lambda x: x["date"])
    return items


def vote_pdf_links(article_url, cache_dir=None):
    html = _get(article_url, cache_dir)
    out = []
    for m in re.finditer(r'<a[^>]+href="(https://bip\.zielonagora\.pl/system/pobierz\.php[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        if "Wyniki głosowania" in re.sub(r"<[^>]+>", " ", m.group(2)):
            out.append(m.group(1).replace("&amp;", "&"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    uch = crawl_list(cache)
    print(f"[zg] uchwały IX kad: {len(uch)} ({uch[0]['date']} .. {uch[-1]['date']})")

    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(exist_ok=True)
    records = []
    n_pdf = n_ok = n_fail = n_nopdf = 0
    for i, u in enumerate(uch):
        if args.limit and i >= args.limit:
            break
        pf = pdf_dir / (u["num"].replace(".", "_") + ".pdf")
        if not (pf.is_file() and pf.stat().st_size > 800):
            if args.skip_download:
                continue
            links = vote_pdf_links(u["url"], cache)
            if not links:
                n_nopdf += 1
                continue
            data = _get(links[0], cache, binary=True)
            if data[:4] != b"%PDF":
                n_nopdf += 1
                continue
            pf.write_bytes(data)
        recs = _print_parser.records_from_pdf(pf.read_bytes())
        for r in recs:
            r["date"] = u["date"]; r["num"] = u["sesja"]
            if u["title"] and (not r.get("topic") or r["topic"] == "(glosowanie)"):
                r["topic"] = u["title"]
        nok = sum(1 for r in recs if r["ok"])
        n_pdf += len(recs); n_ok += nok; n_fail += len(recs) - nok
        recs = [r for r in recs if r["ok"]]
        records.extend(recs)
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(uch)}] votes {n_pdf} ok {n_ok} nopdf {n_nopdf}", flush=True)

    print(f"[zg] głosy {n_ok}/{n_pdf} (odrzucone {n_fail}, bez PDF {n_nopdf})")
    out, tv, ts = _print_parser.build_output(records, club_assign)
    profiles = _print_parser.build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(exist_ok=True)
    kad_obj = out["kadencje"][0]
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(kad_obj, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data_json = {
        "city_name": cfg.get("city_name", "Zielona Góra"),
        "rada_name": cfg.get("rada_name", "Rada Miasta Zielona Góra"),
        "generated": out["generated"],
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "stats": {"total_votes": tv, "total_sessions": ts,
                  "total_councilors": out["kadencje"][0]["total_councilors"]},
    }
    (docs / "data.json").write_text(json.dumps(data_json, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zg] OK votes={tv} sessions={ts} councilors={data_json['stats']['total_councilors']}")


if __name__ == "__main__":
    main()
