#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Łapy — imienne głosowania Rady Miejskiej w Łapach (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Łapach na regionalnej platformie podlaskiej
(bip-umlapy.podlaskie.eu). Sekcja Rada Miejska → Głosowania → Kadencja IX (2024-2029)
publikuje per sesja PDF "Głosowania NN sesja Rady Miejskiej w Łapach w dniu …"
(tekstowy, generowany przez app.esesja.pl — "Raport z głosowań") z głosowaniami
imiennymi per radny (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI)
oraz nagłówkiem agregatu ("wyniki: ZA: N, PRZECIW: M, …") do walidacji.

Format PDF: eSesja imienne INLINE ("Wyniki imienne: Imię Nazwisko (ZA), …") oraz
wariant blokowy ("Wyniki imienne:\\nZA (N)\\nImię Nazwisko, …"). Oba pokrywane.

Walidacja per głos: zsumowane głosy imienne == liczniki z nagłówka (ZA/PRZECIW).

Skład rady (21 radnych) z BIP "Skład Rady Miejskiej IX kadencji". Kluby radnych
NIE są publikowane w BIP Łap — wszyscy oznaczani NZ (PENDING kuratorowania).

Użycie:
    python scrape_lapy.py --city-dir <cities/lapy> [--cache-dir dir]
Zapisuje: docs/kadencja-2024-2029.json, docs/data.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip-umlapy.podlaskie.eu"
LISTING = "/Rada_1b7bc685a35263e/kadencja-ix-2024-2029.html"
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

_MONTH_PL = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
             'lipca': 7, 'sierpnia': 8, 'wrzesnia': 9, 'pazdziernika': 10, 'listopada': 11,
             'grudnia': 12, 'września': 9, 'października': 10}

def _norm_diac(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY: time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".pdf")
        if cf.is_file(): return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir: (cache_dir / (key + ".pdf")).write_bytes(data)
    return data

# ---------------- discovery ----------------
def parse_session_from_title(title):
    """'Głosowania IV sesja Rady Miejskiej w Łapach w dniu 30.09.2024 r..pdf' ->
    (num, date). Roman + date (genitive month OR D.M.YYYY)."""
    m = re.search(r'([IVXLCDM]+)\s+(?:nadzwyczajna\s+)?sesja', title)
    if not m: return None, None
    num = roman_to_int(m.group(1))
    date = None
    dm = re.search(r'w dniu\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', title)
    if not dm:
        dm = re.search(r'sesja\s+Rady\s+Miejskiej\s+w\s+Łapach\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', title)
    if dm:
        d, mo, y = dm.group(1), dm.group(2), dm.group(3)
        date = f"{y}-{int(mo):02d}-{int(d):02d}"
    else:
        gm = re.search(r'w dniu\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', title, re.I)
        if gm:
            mon = gm.group(2).lower()
            mo = _MONTH_PL.get(mon) or _MONTH_PL.get(_norm_diac(mon))
            if mo:
                date = f"{gm.group(3)}-{mo:02d}-{int(gm.group(1)):02d}"
    return num, date

def discover_sessions(cache_dir):
    t = _get(BIP + LISTING, cache_dir).decode("utf-8", "ignore")
    sessions = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href="([^"]*?/resource/\d+/[^"\s]+?\.(?:pdf|PDF))"[^>]*>(.*?)</a>', t, re.S):
        href, label = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not label.lower().startswith('głosowan'): continue
        if href in seen: continue
        seen.add(href)
        if href.startswith('/'):
            href = BIP + href
        num, date = parse_session_from_title(label)
        sessions.append({"num": num, "date": date, "url": href})
    sessions = [s for s in sessions if s["date"] and s["date"] >= KAD_START and s["num"]]
    sessions.sort(key=lambda s: s["date"])
    return sessions

# ---------------- eSesja imienne parsing (inline + blocks) ----------------
_VOTE_MAP = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
             'WSTRZYMUJE SIĘ': 'wstrzymal_sie', 'BRAK GŁOSU': 'brak_glosu',
             'NIEOBECNI': 'nieobecni'}

def _norm_name(n):
    n = re.sub(r'\s+', ' ', n).strip(' .,;:')
    return n.strip()

def parse_pdf(data):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return [], 0
    lines = full.split("\n")
    secs = [i for i, l in enumerate(lines) if re.match(r'^\s*\d+\.\s*(Głosowano|Głosowanie)', l)]
    votes = []
    n_fail = 0
    for k, start in enumerate(secs):
        end = secs[k + 1] if k + 1 < len(secs) else len(lines)
        sec = "\n".join(lines[start:end])
        tm = re.search(r'(?:Głosowano\s+w\s+sprawie:?\s*|Głosowanie\s+w\s+sprawie\s+)'
                       r'(.*?)(?:\s*-\s*czas głosowania|\s*czas głosowania)', sec, re.S | re.I)
        topic = _norm_name(tm.group(1)) if tm else f"Głosowanie {k + 1}"
        agg = re.search(r'wyniki:\s*ZA:\s*(\d+),\s*PRZECIW:\s*(\d+)', sec)
        agg_t = (int(agg.group(1)), int(agg.group(2))) if agg else None
        za, pr, wz, bg, nb = [], [], [], [], []
        im = re.search(r'wyniki imienne:\s*(.*)', sec, re.S | re.I)
        parsed = False
        if im:
            seg = im.group(1)
            seg = re.sub(r'(?im)^\s*Wygenerowano[^\n]*\n?', '', seg)
            seg = re.sub(r'(?m)^\s*\d{4}-\d{2}-\d{2}\s*(\d{2}:\d{2}:\d{2})?\s*\n?', '', seg)
            seg = re.split(r'\n\s*Uczestnictwo', seg, maxsplit=1)[0]
            if re.search(r'^\s*ZA\s*\(\s*\d+\s*\)', seg):
                # Format B — bloki
                body = re.sub(r'wyniki imienne:?', '', seg, flags=re.I)
                heads = list(re.finditer(
                    r'^(?:ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\(\s*\d+\s*\)\s*$',
                    body, re.M))
                for hi, h in enumerate(heads):
                    lab = h.group(0).split('(')[0].strip()
                    st = h.end()
                    en = heads[hi + 1].start() if hi + 1 < len(heads) else len(body)
                    chunk = re.sub(r'Wygenerowano.*', '', body[st:en], flags=re.S)
                    names = [_norm_name(x) for x in chunk.split(',')
                             if _norm_name(x) and not _norm_name(x).startswith('Wygenerowano')]
                    key = _VOTE_MAP.get(lab)
                    if key:
                        {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz, 'brak_glosu': bg, 'nieobecni': nb}[key].extend(names)
                parsed = True
            else:
                # Format A — inline "Name (VOTE)"
                marker = (r'(?:ZA|PRZECIW|WSTRZYMUJĘ\s+SIĘ|WSTRZYMUJE\s+SIĘ|'
                          r'BRAK\s+GŁOSU|NIEOBECNI)')
                for m in re.finditer(r'([^,]+?)\s*\((' + marker + r')\)', seg, re.S):
                    nm = _norm_name(m.group(1))
                    key = _VOTE_MAP.get(" ".join(m.group(2).split()))
                    if nm and key:
                        {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz, 'brak_glosu': bg, 'nieobecni': nb}[key].append(nm)
                parsed = True
        named = {"za": za, "przeciw": pr, "wstrzymal_sie": wz, "brak_glosu": bg, "nieobecni": nb}
        n_total = sum(len(x) for x in named.values())
        if parsed and agg_t and n_total:
            # validate ZA & PRZECIW counts against header aggregates (brak/nieobecni toleracja editów źródła)
            ok = agg_t[0] == len(za) or agg_t[1] == len(pr)
            if not (agg_t[0] == len(za) and agg_t[1] == len(pr)):
                n_fail += 1
        votes.append({"topic": topic, "agg": agg_t, "named": named})
    return votes, n_fail

# ---------------- output ----------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
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
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
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
            "votes_nieobecny": 0, "votes_with_club": 0, "votes_against_club": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                if name not in councilors_data: continue
                c = councilors_data[name]
                if cat == "za": c["votes_za"] += 1
                elif cat == "przeciw": c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie": c["votes_wstrzymal"] += 1
                elif cat == "nieobecni": c["votes_nieobecny"] += 1
                else: c["votes_brak"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for n in names: councillor_sess[n].add(v["session_date"])
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
            for name in v["named_votes"].get(cat, []): vectors[name][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10: continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, "NZ"), "club_b": club_assign.get(b, "NZ"),
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
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0, "nieobecny": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START: continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal_sie" if cat == "wstrzymal_sie" else "nieobecny" if cat == "nieobecni" else "brak"
                cv[name][key] += 1
                cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    sessions_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sessions_set) or 1
    for name in sorted(set(list(cv.keys()) + list(roster))):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak", "nieobecny")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 100.0 * present_sess / n_sessions
        aktywn = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) / n_sessions * 100
        profiles.append({"name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(name, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                "zgodnosc_z_klubem": 0.0, "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecny"], "votes_total": total, "rebellion_count": 0,
                "rebellions": [], "roles": [], "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    work_dir = Path(args.work_dir) if args.work_dir else city_dir / "work"
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}
    roster = set(cfg.get("councilor_roster", []))

    sessions = discover_sessions(cache)
    print(f"[lapy] {len(sessions)} sesji IX kad. (2024-2029)")
    records = []; n_fail_total = 0
    for se in sessions:
        data = _get(se["url"], cache)
        recs, n_fail = parse_pdf(data)
        n_fail_total += n_fail
        for r in recs:
            r["date"] = se["date"]; r["num"] = se["num"]
        records += recs
        print(f"  [ok] {se['date']} nr{se['num']} votes={len(recs)} (agg_mismatch={n_fail})")
    print(f"[lapy] total records={len(records)} agg-fail-batches={n_fail_total}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign, roster)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[lapy] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
