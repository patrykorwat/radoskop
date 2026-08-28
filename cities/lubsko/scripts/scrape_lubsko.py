#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Lubsko — imienne głosowania Rady Miejskiej w Lubsku (IX kadencja 2024-2029).

Źródło: BIP bip.lubsko.pl (custom eBOI/idcom-jst-style CMS, /{id}/{slug}/), strona
"Protokoły z głosowań kadencja IX" (/343/). Dla KAŻDEJ sesji IX kadencji (I…XXX,
07.05.2024…14.07.2026) publikowany jest załącznik PDF "Protokół ...sesji..."
("Protokol_Rada24_posiedzenie_{N}.pdf") w klasycznym eSesja imiennym FORMACIE TEKSTOWYM:

    <temat punktu ...>
    Wyniki głosowania: ZA (15), PRZECIW (0), WSTRZYMUJĘ SIĘ (0), BRAK GŁOSU (0), NIEOBECNY (0)
    Lista imienna
    ZA: <imię nazwisko, ...>
    PRZECIW:
    WSTRZYMUJĘ SIĘ:
    BRAK GŁOSU:
    NIEOBECNY:
    ID głosowania: ..., czas zakończenia: ...

Skład = pełny zbiór unikalnych radnych z głosowań (potwierdzony na stronie BIP /323/Radni/
— 15 radnych Rady Miejskiej w Lubsku). Kluby: BIP nie publikuje klubów radnych → NZ.
Walidacja per głos: liczba nazwisk w liste imiennej == licznik z nagłówka ZA (N) itd.

Uwaga: niektóre sesje mają dodatkowe "Notatka/Informacja do protokołu" (głosowanie ręczne
po awarii tabletów) — zawierają one uzupełniające głosowania poza głównym protokołem.
Scraper opiera się na głównych protokołach per-sesja (kanoniczny rejestr głosowań imiennych).

Użycie:
    python scrape_lubsko.py --city-dir <cities/lubsko> [--work-dir dir] [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from html import unescape

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.lubsko.pl"
LISTING = "/343/Protokoly_z_glosowan_kadencja_IX/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.5
_LAST = 0.0

_ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
def roman_to_int(s):
    tot = 0; prev = 0
    for ch in reversed(s.upper()):
        v = _ROMAN.get(ch, 0)
        if v < prev: tot -= v
        else: tot += v; prev = v
    return tot

def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY: time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file(): return cf.read_bytes()
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir: (cache_dir / (key + ".dat")).write_bytes(data)
            return data
        except (ConnectionError, Timeout, OSError) as e:
            if attempt == 5: raise
            time.sleep(3 + attempt * 4)
    raise RuntimeError(f"GET failed: {url}")

# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """Parse /343/ listing: for each session attachment get title + pdf url."""
    t = _get(BIP + LISTING, cache_dir).decode("utf-8", "ignore")
    sessions = []
    for m in re.finditer(
            r'<a href="(https://bip\.lubsko\.pl/system/pobierz\.php\?plik=([^"&]+)(?:&amp;|&)id=[a-f0-9]{32}[^"]*)"[^>]*title="Pobierz załącznik">(.*?)</a>',
            t, re.S):
        url = m.group(1)
        plik = m.group(2)
        title = re.sub(r'\s+', ' ', html_unescape(re.sub(r'<[^>]+>', ' ', m.group(3)))).strip()
        if 'Protokol_Rada24_posiedzenie_' not in plik:
            # some sessions use custom filenames (e.g. protokol_z_XXV_sesji.pdf)
            if not re.match(r'^protokol', plik, re.I) and 'protokol' not in plik.lower():
                continue
        # parse roman numeral + date from title: "IX sesja ... w dniu 19.12.2024r."/"z dnia 27 czerwca 2024r."
        rm = re.match(r'([IVXLCDM]+)\s+sesja', title)
        num = roman_to_int(rm.group(1)) if rm else None
        date = None
        # accept both 4-digit and 2-digit years: w dniu 23.05.24r / w dniu 13.09.2024r / z dnia 29.05.2025r
        dm = re.search(r'(?:w dniu|z dnia)\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})', title)
        if dm:
            d, mo, y = dm.group(1), dm.group(2), dm.group(3)
            if len(y) == 2:
                y = "20" + y
            date = f"{y}-{int(mo):02d}-{int(d):02d}"
        else:
            dm2 = re.search(r'z dnia (\d{1,2}) (\w+) (\d{4})', title)
            if dm2:
                d, mon, y = dm2.group(1), dm2.group(2), dm2.group(3)
                mnum = parse_month(mon)
                date = f"{y}-{mnum:02d}-{int(d):02d}"
        if num is None or date is None:
            continue
        if date < KAD_START: continue
        sessions.append({"num": num, "date": date, "title": title, "pdf_url": unescape(url)})
    # dedupe by num (one protocol per session)
    seen = {}
    for s in sessions:
        seen[s["num"]] = s
    sessions = sorted(seen.values(), key=lambda s: s["date"])
    return sessions

_MONTHS = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,
           'lipca':7,'sierpnia':8,'września':9,'wrzesnia':9,'października':10,
           'pazdziernika':10,'listopada':11,'grudnia':12}
def parse_month(s):
    return _MONTHS.get(s.strip().lower(), 1)

def html_unescape(s):
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&quot;', '"')
    try:
        from html import unescape as _u; return _u(s)
    except Exception:
        return s

# ---------------- eSesja imienne TEXT parsing (Lubsko variant) ----------------
_CAT_ORDER = [("ZA", "za"), ("PRZECIW", "przeciw"), ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
              ("BRAK GŁOSU", "brak"), ("NIEOBECNY", "nieobecni")]
_FOOTER_RE = re.compile(r'(wygenerowano|za\s*pomocą|app\.esesja\.pl|urząd miejski|przewodniczący rady)', re.I)

def _clean_name(s):
    s = re.sub(r'\s+', ' ', s.strip().strip('.,;'))
    if not s: return None
    if not any(ch.isalpha() for ch in s): return None
    if _FOOTER_RE.search(s): return None
    if re.search(r'\d{1,2}:\d{2}', s): return None
    return s

def _norm_name(s):
    return re.sub(r'\s+', ' ', re.sub(r'\s*-\s*', '-', s)).strip()

def _name_to_re(name):
    tokens = name.split()
    out = []
    for tok in tokens:
        out.append(tok.replace('-', '-\\s*'))
    return r'\s+'.join(out)

def _make_roster_re(roster):
    canon = {}
    patterns = []
    for name in roster:
        canon[_norm_name(name).lower()] = name
        patterns.append(_name_to_re(name))
    if not canon:
        return None, {}
    patterns.sort(key=len, reverse=True)
    return re.compile("|".join(patterns)), canon

def _names_in_region(region, roster_re, canon):
    found = []
    seen = set()
    if roster_re is None:
        return found
    for m in roster_re.finditer(region):
        key = _norm_name(m.group(0)).lower()
        if key not in seen:
            seen.add(key)
            found.append(canon.get(key, m.group(0)))
    return found

def parse_pdf(data, roster):
    """Parse a Lubsko protocol PDF -> list of records with named votes, validated."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return [], 1
    text = re.sub(r'-\s+', '-', text)  # repair hyphen wraps
    markers = list(re.finditer(r'Wyniki głosowania:', text))
    if not markers:
        return [], 0
    records = []
    n_fail = 0
    roster_re, canon = _make_roster_re(list(roster))
    for i, m in enumerate(markers):
        start = m.start()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        block = text[start:end]
        hm = re.search(r'Wyniki głosowania:\s*ZA\s*\((\d+)\),\s*PRZECIW\s*\((\d+)\),\s*WSTRZYMUJĘ SIĘ\s*\((\d+)\),\s*BRAK GŁOSU\s*\((\d+)\),\s*NIEOBECNY\s*\((\d+)\)', block)
        if not hm:
            n_fail += 1
            continue
        za_c, p_c, w_c, b_c, n_c = (int(x) for x in hm.groups())
        counts = {"za": za_c, "przeciw": p_c, "wstrzymal_sie": w_c, "brak": b_c, "nieobecni": n_c}
        named = {}
        ok = True
        for label, cat in _CAT_ORDER:
            lm = re.search(r'^\s*(?:(?:Lista\s+imienna)\s*)?' + label + r'\s*:(.*?)(?=^\s*(?:(?:Lista\s+imienna)\s*)?(?:ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNY)\s*:|\Z)', block, re.M | re.S)
            names = []
            if lm:
                raw = lm.group(1)
                names = _names_in_region(raw, roster_re, canon)
            names = names[:counts[cat]]
            if len(names) != counts[cat]:
                ok = False
            named[cat] = names
        # Validate: total named across za/against/wstrzym/brak == za+przeciw+wstrzym+brak header (excl nieobecni)
        sum_found = sum(len(named[c]) for c in ("za", "przeciw", "wstrzymal_sie", "brak"))
        sum_hdr = counts["za"] + counts["przeciw"] + counts["wstrzymal_sie"] + counts["brak"]
        if sum_found != sum_hdr or not ok:
            n_fail += 1
        records.append({"counts": counts, "named": named})
    return records, n_fail

# ---------------- output ----------------
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug = name.lower()
    for pl, a in repl.items(): slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START: continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0,
                                   "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values(): all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data: continue
                if cat == "nieobecni": councilors_data[nm]["votes_nieobecny"] += 1
                elif cat == "brak": councilors_data[nm]["votes_brak"] += 1
                elif cat == "za": councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw": councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie": councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names: councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []): vectors[nm][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10: continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None, roster=None):
    club_assign = club_assign or {}
    roster = roster or set()
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0,
                              "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START: continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]: cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(set(list(cv.keys()) + list(roster))):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak"))
        total = total or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    work_dir = Path(args.work_dir) if args.work_dir else city_dir / "work"
    pdf_dir = work_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}
    roster = set(cfg.get("councilor_roster", []))

    sessions = discover_sessions(cache)
    print(f"[lubsko] {len(sessions)} sesji IX kad. (2024-2029)")
    records = []
    n_fail_total = 0
    for se in sessions:
        url = se["pdf_url"]
        fname = re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_") + ".pdf"
        pdf_path = pdf_dir / fname
        data = _get(url, cache)
        pdf_path.write_bytes(data)
        recs, n_fail = parse_pdf(data, list(roster))
        n_fail_total += n_fail
        for r in recs:
            r["date"] = se["date"]; r["num"] = se["num"]
        records += recs
        print(f"  [ok] {se['date']} nr{se['num']} votes={len(recs)} (frag_bad={n_fail})")
    print(f"[lubsko] total records={len(records)} parse-fails={n_fail_total}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign, roster)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[lubsko] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
