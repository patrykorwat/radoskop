#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Olsztynek — imienne głosowania Rady Miejskiej w Olsztynku (BIP JetBrains/phocadownload).

Źródło: https://bip.olsztynek.pl — Rada Miejska kadencja 2024-2029 → Sesje Rady Miejskiej
(I..XXXV). Każda sesja ma kategorię phocadownload z plikami 'Sesja N Głosowanie K.pdf'
(per-głosowanie, 1 strona, format 'APWINC II') + 'Protokół...' PDF. Pliki głosowań
mają WARSTWĘ TEKSTOWĄ: nagłówek (Numer osoby/toks: e.g. '84\n7. Głosowanie na składem
Komisji Skrutacyjnej'), Data głosowania DD.MM.YYYY HH:MM, agregaty Głosy za/przeciw/
wstrzymujące się + tabela 2-kolumnowa Lp. Nazwisko i imię → Głos (ZA / PRZECIW /
WSTRZYMUJĘ SIĘ / NIEOBECNA / NIEOBECNY / BRAK).
175 glosowań: 166 OK (2024-05-07..2026-07-16), 9 bez agregatów → wykluczone.
Roster: 15 radnych z tabel w PDF (uprawnieni). Interpelacje: BIP /2800-menu/.../interpelacje-radnych.html.

Użycie: python scrape_olsztynek.py --city-dir cities/olsztynek [--cache-dir .cache]
"""
import argparse, hashlib, json, re, sys, time
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_names_pl import fix_all as _fix_all_names  # noqa: E402

BASE = "https://bip.olsztynek.pl"
IDX = f"{BASE}/3939-menu/wadze-gminy-olsztynek/rada-miejska-kadencja-2024-2029/sesje-rady-miejskiej-w-olsztynku2024-2029.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.45
_LAST = 0.0

ROMANS_ORDER = {"i-sesja": 1, "ii-sesja": 2, "iii-sesja": 3, "iv-sesja": 4, "v-sesja": 5,
    "vi-sesja": 6, "vii-sesja": 7, "viii-sesja": 8, "ix-sesja": 9, "x-sesja": 10,
    "xi-sesja": 11, "xii-sesja": 12, "xiii-sesja": 13, "xiv-sesja": 14, "xv-sesja": 15,
    "xvi-sesja": 16, "xvii-sesja": 17, "xviii-sesja": 18, "xix-sesja": 19, "xx-sesja": 20,
    "xxi-sesja": 21, "xxii-sesja": 22, "xxiii-sesja": 23, "xxiv-sesja": 24, "xxv-sesja": 25,
    "xxvi-sesja": 26, "xxvii-sesja": 27, "xxviii-sesja": 28, "sesja-xxix": 29, "sesja-xxx": 30,
    "sesja-xxxi": 31, "xxxii-sesja": 32, "xxxiii-sesja": 33, "xxxiv-sesja": 34, "xxxv-sesja": 35}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    ext = ".bin" if binary else ".html"
    if cache is not None:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


def _roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = vals[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def discover_sessions(cache=None):
    """Lista sesji: href='/3xxx-menu/.../xyz-sesja...' + Roman Numerals from title."""
    html = _fetch(IDX, cache)
    sess = []
    seen = set()
    for m in re.finditer(r'href="(/(?:3\d{3})-menu/wadze-gminy-olsztynek/rada-miejska-kadencja-2024-2029/sesje-rady-miejskiej-w-olsztynku[^"]+\.html)"', html):
        h = m.group(1)
        if h in seen:
            continue
        seen.add(h)
        key = re.search(r"/(3\d{3})-menu", h).group(1)
        sess.append({"key": key, "path": h})
    return sess


def discover_cats(sess_list, cache=None):
    """Per sesja page → phocadownload category path."""
    cats = {}
    for s in sess_list:
        html = _fetch(BASE + s["path"], cache)
        m = re.search(r'href="(/component/phocadownload/category/(\d+)-[^"]+\.html)"', html)
        if m:
            catpath = m.group(1)
            title_m = re.search(r"<title>([^<]+)</title>", html)
            title = title_m.group(1).strip() if title_m else s["key"]
            num = None
            for k, n in ROMANS_ORDER.items():
                if k in catpath:
                    num = n
                    break
            if num is None:
                m2 = re.search(r"\b([ivxlcdm]{1,5})\b", catpath, re.I)
                num = _roman_to_int(m2.group(1)) if m2 else None
            cats[s["key"]] = {"catpath": catpath, "num": num, "title": title}
    return cats


def download_files(catpath, cache=None):
    """Files in a phocadownload category: 'Sesja N Głosowanie K' + 'Protokół...'."""
    html = _fetch(BASE + catpath, cache)
    files = []
    for m in re.finditer(r"pd-title\">([^<]+)</div>.{0,400}?href=\"(/component/phocadownload/category/[^\"]+download=(\d+):[^\"]+)\"", html, re.S):
        files.append({"title": m.group(1).strip(), "href": m.group(2)})
    return files


def parse_pdf_bytes(content):
    doc = pymupdf.open(stream=content, filetype="pdf")
    t = doc[0].get_text() if doc.page_count else ""
    dm = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
    date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
    t_colon = re.search(r"Glosowanie:?\s*([\w\s]{3,80})", t)
    tm = re.search(r"^\s*\d+\s*\n\s*\d+\.\s*(.+)$", t, re.M)
    topic = tm.group(1).strip() if tm else ""
    za = re.search(r"Głosy za\s*\n(\d+)", t)
    pr = re.search(r"Głosy przeciw\s*\n(\d+)", t)
    wz = re.search(r"Głosy wstrzymujące się\s*\n(\d+)", t)
    named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
    body_idx = t.find("Uprawnieni do głosowania")
    if body_idx < 0:
        return None
    body = t[t.find("Uprawnieni do głosowania"):]
    rows = re.findall(r"^\s*(\d+)\.\s*\n([A-ZŁŚŻŹĄĆĘ][^0-9\n]+?)\n(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|NIEOBECNA|NIEOBECNY|NIEOBECNI|BRAK)", body, re.M)
    if not rows:
        return None
    for _lp, name, vote in rows:
        n = name.strip()
        v = vote.strip().upper()
        if v == "ZA":
            named["za"].append(n)
        elif v == "PRZECIW":
            named["przeciw"].append(n)
        elif v.startswith("WSTRZYM"):
            named["wstrzymal_sie"].append(n)
    for cat in named:
        named[cat] = _fix_all_names(named[cat])
    agg = (int(za.group(1)) if za else None, int(pr.group(1)) if pr else None,
           int(wz.group(1)) if wz else None)
    got = (len(named["za"]), len(named["przeciw"]), len(named["wstrzymal_sie"]))
    ok = all(x is not None for x in agg) and agg == got
    return {"topic": topic, "date": date, "named": named, "agg": agg, "got": got, "ok": ok}


def _slugify(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sess_list = discover_sessions(cache)
    cats = discover_cats(sess_list, cache)
    print(f"[olsztynek] sesje z kategoriałami: {len(cats)}")
    records = []
    sessions_map = {}
    skipped = 0
    total_ok = 0
    for key in sorted(cats, key=lambda k: int(k)):
        c = cats[key]
        files = download_files(c["catpath"], cache)
        glosy = [f for f in files if re.search(r"Głosowanie|Glosowanie", f["title"], re.I)]
        if not glosy:
            continue
        sdate = None
        for g in glosy:
            vno_m = re.search(r"Głosowanie\s*(\d+)", g["title"], re.I)
            vno = int(vno_m.group(1)) if vno_m else 0
            content = _fetch(BASE + g["href"], cache, binary=True)
            p = parse_pdf_bytes(content)
            if not p or not p["date"] or p["date"] < KAD_START:
                continue
            if not p["ok"]:
                skipped += 1
                print(f"  [skip] {g['title']}: agg={p['agg']} got={p['got']}")
                continue
            records.append({"topic": p["topic"], "named": p["named"],
                            "counts": {"za": len(p["named"]["za"]),
                                       "przeciw": len(p["named"]["przeciw"]),
                                       "wstrzymal_sie": len(p["named"]["wstrzymal_sie"])},
                            "session_date": p["date"], "vno": vno})
            total_ok += 1
            sdate = p["date"]
        if sdate:
            sessions_map[sdate] = c["title"]

    data, kad = build_output(records, sessions_map)
    profiles = build_profiles(records)
    save_split(data, kad, city_dir / "docs", profiles)
    print(f"[olsztynek] votes={kad['total_votes']} sessions={kad['total_sessions']} "
          f"councilors={kad['total_councilors']}; skipped (nie-reconcilowane): {skipped}")


def build_output(records, sessions_map):
    sessions_by_date = {}
    all_votes = []
    vid = 0
    for rec in records:
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": sessions_map.get(d, d),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": sessions_map.get(d, d), "topic": rec.get("topic", ""),
                          "named_votes": rec["named"], "counts": rec["counts"]})
    sessions_data = [{"date": d, "number": sessions_by_date[d]["number"],
                      "vote_count": sessions_by_date[d]["vote_count"],
                      "attendee_count": len(sessions_by_date[d]["attendees"]),
                      "attendees": sorted(sessions_by_date[d]["attendees"]), "speakers": []}
                     for d in sorted(sessions_by_date)]
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    cc = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if cat == "za":
                    cc[nm]["za"] += 1
                elif cat == "przeciw":
                    cc[nm]["przeciw"] += 1
                else:
                    cc[nm]["wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    c_session = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c_session[nm].add(v["session_date"])
    councilors_list = []
    for nm in sorted(cc):
        present = sum(cc[nm].values())
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(c_session[nm]) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({
            "name": nm, "club": "", "district": None, "role": "",
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc[nm]["za"], "votes_przeciw": cc[nm]["przeciw"],
            "votes_wstrzymal": cc[nm]["wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None,
        })
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors), 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for vid2 in common if vectors[a][vid2] == vectors[b][vid2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}, kad


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    sess = defaultdict(set)
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                sess[nm].add(d)
    n_sessions = len({r.get("session_date") for r in records if r.get("session_date", "") >= KAD_START}) or 1
    total_records = sum(1 for r in records if r.get("session_date", "") >= KAD_START)
    profiles = []
    for nm in sorted(cv):
        vd = cv[nm]
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, total_records) * 100
        frekwencja = len(sess[nm]) / n_sessions * 100
        profiles.append({"name": nm, "slug": _slugify(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": "", "role": "",
                             "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywn, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": sum(vd.values()),
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"scraped_at": datetime.now().isoformat(), "profiles": profiles, "total": len(profiles)}


def save_split(data, kad, out_dir, profiles):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
