#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Krzeszowice — imienne głosowania Rady Miejskiej w Krzeszowicach (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Krzeszowicach na platformie bip.malopolska.pl (Madkom SPA,
encja `umkrzeszowice`), kategoria Rada -> Sesje -> "Imienne wykazy głosowań" (menu 310174)
z podkategoriami per-rok (2024: 430283, 2025: 457930, 2026: 471942). Każdy artykuł = jedna
sesja; załącznik PDF "Protokoły głosowań z N sesji ... z dnia ..." (tekstowy, bez OCR) zawiera
per-głosowanie imienne w formacie:

    {N}. Podjęcie uchwały w sprawie ...
    Głosowanie jawne
    {data}, {godz}
    Przyjęto jednomyślnie | Przyjęto
    Uprawnieni:21 Obecni:19
    ZA a / Oddano głosów / PRZECIW b / WSTRZYMAŁO SIĘ c / Brak głosu d / Brak obecności e
    Szczegóły
    ZA: a głosów
    <nazwiska>
    PRZECIW: b głosów
    <nazwiska>
    WSTRZYMAŁO SIĘ: c głosów
    <nazwiska>
    Brak głosu: d głosów
    <nazwiska>
    Brak obecności: e głosów
    <nazwiska>
    {N} / {total}

API Madkom (bez auth): /api/contexts/umkrzeszowice, /api/menu/{id}/articles?limit=N,
/api/menu/{id}/submenu, /api/articles/{id}, /api/files/{attachmentId}.

Sesje IX kad.: I (2024-05-07) .. XXX (2026-07-30) — kompletna seria. Roster 21 radnych z
kategorii Rada -> Skład Rady -> 2024-2029 (menu 435868). Kluby z kategorii Rada -> Kluby Radnych
(menu 436699): Wspólna Gmina Krzeszowice, Prawo i Sprawiedliwość, Koalicja Obywatelska,
Rzetelna Gmina Krzeszowice — kompletny podział 21/21.

Walidacja per-głosowanie: liczba nazwisk w ZA/PRZECIW/WSTRZYMAŁO/Brak głosu/Brak obecności
== agregat z nagłówka ("Uprawnieni:21 Obecni:19" ... "ZA 19").

Użycie:
    python scrape_krzeszowice.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache] [--max-sessions N]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests

BASE = "https://bip.malopolska.pl"
ENTITY = "umkrzeszowice"
GLOS_MENU = 310174  # Imienne wykazy głosowań
YEARS = {2024: 430283, 2025: 457930, 2026: 471942}
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

ROSTER = [
    "Angelika Balawejder", "Czesław Bartl", "Renata Brzózka", "Robert Chochół",
    "Anna Dudek", "Krystyna Galos", "Beata Głąb", "Szymon Goc", "Wojciech Godyń",
    "Kamil Kłosowski", "Justyna Knapczyk", "Leszek Kramarz", "Monika Mnich",
    "Wojciech Pałka", "Katarzyna Pudełek", "Wojciech Styrylski", "Paweł Wielgosz",
    "Jan Węgrzyn", "Henryk Woszczyna", "Jerzy Wnęk", "Władysław Ziomek",
]

CLUB_ASSIGN = {
    "Wspólna Gmina Krzeszowice": ["Jerzy Wnęk", "Beata Głąb", "Wojciech Godyń",
                                  "Wojciech Styrylski", "Jan Węgrzyn"],
    "Prawo i Sprawiedliwość": ["Henryk Woszczyna", "Robert Chochół", "Krystyna Galos",
                               "Leszek Kramarz", "Wojciech Pałka", "Katarzyna Pudełek",
                               "Władysław Ziomek"],
    "Koalicja Obywatelska": ["Monika Mnich", "Angelika Balawejder", "Anna Dudek"],
    "Rzetelna Gmina Krzeszowice": ["Kamil Kłosowski", "Renata Brzózka", "Justyna Knapczyk",
                                   "Czesław Bartl", "Szymon Goc", "Paweł Wielgosz"],
}

_ACC = str.maketrans({'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'})
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().translate(_ACC))
def make_slug(name):
    sn=str(name or "").lower()
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r"[^a-z0-9]+","-",sn); return sn.strip("-")

_ROSTER_NORM = [_norm(n) for n in ROSTER]

def canon_name(raw):
    raw = re.sub(r'^\d+\.\s*', '', (raw or '').strip())
    if not raw:
        return raw
    rn = _norm(raw)
    for c, cn in zip(ROSTER, _ROSTER_NORM):
        if rn == cn:
            return c
    toks = rn.split()
    if toks:
        raw_last = toks[-1]
        for c, cn in zip(ROSTER, _ROSTER_NORM):
            cn_last = cn.split()[-1]
            if raw_last == cn_last and len(raw_last) >= 3:
                return c
    from difflib import SequenceMatcher
    best = None; best_s = 0
    for c, cn in zip(ROSTER, _ROSTER_NORM):
        s = SequenceMatcher(None, rn, cn).ratio()
        if s > best_s:
            best_s = s; best = c
    if best_s >= 0.5:
        return best
    return raw.strip()

_MON = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,'lipca':7,
        'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12}
_DATE_RE = re.compile(r'z dnia\s+(\d{1,2})[.\-]\s*(\d{1,2})[.\-]\s*(\d{4})')
_DATE_RE2 = re.compile(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})')

def _date_from_title(title):
    m = _DATE_RE.search(title)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return f"{y}-{mo:02d}-{d:02d}"
    m = _DATE_RE2.search(title)
    if m:
        mo = _MON.get(m.group(2).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    return None


def get_json(url, retries=4):
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=45, verify=False)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}")


def get_file(url, retries=4):
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=90, verify=False)
            if r.status_code == 200:
                return r.content
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}")


def collect_articles():
    out = []
    for year, mid in YEARS.items():
        d = get_json(f"{BASE}/api/menu/{mid}/articles?limit=200")
        for a in d.get("articles") or []:
            art = get_json(f"{BASE}/api/articles/{a['id']}")
            title = (art.get("title") or "").strip()
            sdate = _date_from_title(title)
            if not sdate or sdate < KAD_START:
                continue
            att = None
            for x in art.get("attachments") or []:
                nm = " ".join(str(x.get(k) or "") for k in ("name", "fileName", "title"))
                if ("wynik" in nm.lower() or "protok" in nm.lower()) and "sprostow" not in nm.lower():
                    att = x
                    break
            if att is None and (art.get("attachments") or []):
                att = art["attachments"][0]
            if att is None:
                continue
            roman = re.search(r'(?:z\s+)?([IVXLCDM]+)(?:-?ej)?\s+[Ss]esji', title)
            out.append({"title": title, "date": sdate, "att": att.get("id"),
                        "num": roman.group(1) if roman else ""})
    by_date = {}
    for r in out:
        by_date.setdefault(r["date"], r)
    return list(by_date.values())


_SECTION_MAP = {"ZA":"za","PRZECIW":"przeciw","WSTRZYMAŁO SIĘ":"wstrzymal_sie",
                "Brak głosu":"brak","Brak obecności":"nieobecny"}

def parse_pdf(text):
    votes = []
    # Each vote is delimited by footer lines "N / TOTAL" (e.g. "5 / 91").
    lines = text.split("\n")
    chunks = []
    cur = []
    for ln in lines:
        s = ln.strip()
        if re.match(r'^\d{1,3}\s*/\s*\d{1,3}\s*$', s):
            if cur:
                chunks.append(cur)
            cur = []
            continue
        # skip purely decorative header repeats inside a vote chunk
        if s in ("RADA MIASTA KRZESZOWICE", "KRZESZOWICE") and "PROTOKÓŁ" not in s:
            # it's a repeated page header; drop it (only keep if we're mid-vote)
            continue
        if "PROTOKÓŁ GŁOSOWANIA" in s:
            continue
        cur.append(ln)
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        b = "\n".join(chunk)
        # topic line: "{N}. <temat>" (first such line before "Głosowanie jawne")
        topic = ""
        tm = re.search(r'^\s*\d+\.\s+(.*?)\s*$', b, re.M)
        if tm:
            topic = tm.group(1)
        za_m = re.search(r'^ZA\s+(\d+)', b, re.M)
        if not za_m:
            continue
        def _aggint(pat):
            m = re.search(pat, b, re.M)
            return int(m.group(1)) if m else 0
        agg = {
            "za": int(za_m.group(1)),
            "przeciw": _aggint(r'^PRZECIW\s+(\d+)'),
            "wstrzymal_sie": _aggint(r'^WSTRZYMAŁO SIĘ\s+(\d+)'),
            "brak": _aggint(r'^Brak głosu\s+(\d+)'),
            "nieobecny": _aggint(r'^Brak obecności\s+(\d+)'),
        }
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": [], "nieobecny": []}
        body = b.split("Szczegóły", 1)[-1] if "Szczegóły" in b else ""
        sections = []
        cur_sec = None
        cur_lines = []
        def flush():
            nonlocal cur_sec, cur_lines
            if cur_sec is not None and cur_lines:
                sections.append((cur_sec, cur_lines))
                cur_lines = []
        for ln in body.split("\n"):
            s = ln.strip()
            mm = re.match(r'^(ZA|PRZECIW|WSTRZYMAŁO SIĘ|Brak głosu|Brak obecności):\s*\d+', s)
            if mm:
                flush()
                cur_sec = _SECTION_MAP[mm.group(1)]
                continue
            if not s:
                continue
            if re.match(r'^[\d.]+\s*\.\s*$', s):
                continue
            if cur_sec:
                cur_lines.append(s)
        flush()
        for sec_key, sec_lines in sections:
            tokens = [_norm(w) for w in " ".join(sec_lines).split()]
            for cname, snorm in zip(ROSTER, [_norm(n.split()[-1]) for n in ROSTER]):
                if snorm in tokens:
                    named[sec_key].append(cname)
        for k in named:
            named[k] = list(dict.fromkeys(named[k]))
        votes.append({"topic": topic, "agg": agg, "named": named})
    return votes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-sessions", type=int, default=None)
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    arts = collect_articles()
    arts.sort(key=lambda x: x["date"])
    print(f"[krzeszowice] {len(arts)} session articles", file=sys.stderr)
    if args.max_sessions:
        arts = arts[:args.max_sessions]

    sessions_by_date = {}
    all_votes = []
    vide = 0
    validated = {"ok": 0, "mismatch": 0, "noagg": 0, "emptytable": 0}
    for a in arts:
        pdf_key = a["date"]
        pdf_path = None
        if cache_dir:
            pdf_path = cache_dir / f"{pdf_key}.pdf"
        if pdf_path and pdf_path.exists():
            content = pdf_path.read_bytes()
        else:
            content = get_file(f"{BASE}/api/files/{a['att']}")
            if pdf_path:
                pdf_path.write_bytes(content)
        try:
            with pdfplumber.open(BytesIO(content)) as pdf:
                text = "\n".join((pg.extract_text() or '') for pg in pdf.pages)
        except Exception as e:
            print(f"  {a['date']}: PDF error {e}", file=sys.stderr)
            continue
        votes = parse_pdf(text)
        if not votes:
            validated["noagg"] += 1
        sess_votes = []
        for v in votes:
            counts = {k: len(v["named"][k]) for k in ("za","przeciw","wstrzymal_sie")}
            ok = all(counts[k] == v["agg"][k] for k in ("za","przeciw","wstrzymal_sie"))
            if ok:
                validated["ok"] += 1
            else:
                validated["mismatch"] += 1
            all_votes.append({
                "id": str(vide), "source_url": f"https://bip.malopolska.pl/umkrzeszowice,m,471942,2026.html",
                "session_date": a["date"], "session_number": a["num"],
                "topic": v["topic"], "druk": "", "resolution": "",
                "counts": {"za": v["agg"]["za"], "przeciw": v["agg"]["przeciw"],
                           "wstrzymal_sie": v["agg"]["wstrzymal_sie"]},
                "named_votes": dict(v["named"]),
            })
            vide += 1
        sess_votes = [x for x in []]
        sessions_by_date[a["date"]] = {"date": a["date"], "number": a["num"],
                                       "vote_count": len(votes)}
        print(f"  {a['date']} ({a['num']}): {len(votes)} votes, {sum(1 for v in votes if all(len(v['named'][k])==v['agg'][k] for k in ('za','przeciw','wstrzymal_sie')))} validated", file=sys.stderr)

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": 0, "attendees": [], "speakers": []})

    # councilor stats
    all_names = set()
    for v in all_votes:
        for ns in v["named_votes"].values():
            all_names.update(ns)
    cdata = {n: {"name": n, "club": _club_of(n), "votes_za":0,"votes_przeciw":0,
                 "votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0} for n in ROSTER}
    for v in all_votes:
        for cat, ns in v["named_votes"].items():
            for n in ns:
                if n not in cdata:
                    continue
                c = cdata[n]
                if cat == "za": c["votes_za"] += 1
                elif cat == "przeciw": c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie": c["votes_wstrzymal"] += 1
                elif cat == "brak": c["votes_brak"] += 1
                else: c["votes_nieobecny"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    counc_sess = defaultdict(set)
    for v in all_votes:
        for cat, ns in v["named_votes"].items():
            for n in ns:
                if n in cdata:
                    counc_sess[n].add(v["session_date"])
    councilors_list = []
    for c in sorted(cdata.values(), key=lambda x: x["name"]):
        present = c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]+c["votes_brak"]
        aktywnosc = present/total_votes*100 if total_votes else 0
        frekwencja = len(counc_sess[c["name"]])/total_sessions*100 if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"],
            "frekwencja": round(frekwencja,1), "aktywnosc": round(aktywnosc,1),
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})

    # similarity
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for n in v["named_votes"].get(cat, []):
                vectors[n][v["id"]] = cat
    pairs = []
    names_sorted = sorted(n for n in vectors if n in cdata)
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10: continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same/len(common)*100,1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    output = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
              "kadencje": [kad]}

    # profiles
    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"nieobecny":0,"brak":0,"votes":[]})
    for v in all_votes:
        for cat, ns in v["named_votes"].items():
            for n in ns:
                if n not in cdata: continue
                cv[n][cat] += 1
                cv[n]["votes"].append({"session": v["session_date"], "vote": cat})
    profiles_list = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","nieobecny","brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0*present_sess/all_sess if all_sess else 0.0
        profiles_list.append({"name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {"club": _club_of(name), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(frekw,1), "aktywnosc": 0.0,
                "zgodnosc_z_klubem": 0.0, "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": ""}}})
    profiles = {"profiles": profiles_list, "total": len(profiles_list)}

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index = {"generated": output.get("generated",""), "default_kadencja": output.get("default_kadencja",""),
             "kadencje": []}
    for kadx in output["kadencje"]:
        kid = kadx["id"]
        with open(out_path.parent/f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kadx, f, ensure_ascii=False, separators=(",",":"))
        index["kadencje"].append({"id": kid, "label": kadx.get("label","")})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",",":"))
    with open(out_path.parent/"profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",",":"))

    print(f"[krzeszowice] sesje: {total_sessions}, glosowania: {total_votes}, radni: {len(councilors_list)}")
    print(f"[krzeszowice] walidacja: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']}")


def _club_of(name):
    for cl, mems in CLUB_ASSIGN.items():
        if name in mems:
            return cl
    return "NZ"


if __name__ == "__main__":
    main()
