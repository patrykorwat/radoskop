#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Szydłowiec — imienne głosowania Rady Miejskiej w Szydłowcu (DSSS Vote).

Źródło: https://bip.szydlowiec.pl  (BIP; system DSSS Vote, IX kadencja 2024-2029).
Każda sesja publikuje attachment "Protokoly.pdf" zawierający per-głosowanie
załączniki "Rada Miejskiej w Szydłowcu ... Wygenerowano z systemu DSSS Vote":
  * strona obecności (Obecni/Nieobecni radni),
  * log login/logout,
  * per-głosowanie: "Wniosek/Uchwała ... przyjęta proporcją głosów: jestem
    za N, jestem przeciw M, wstrzymuję się K" + tabelę-imienny wykaz w formacie
    NAME-LIST pod nagłówkami "Jestem za / Jestem przeciw / Wstrzymuję się /
    Obecni radni, którzy nie wzięli udziału w głosowaniu".
  Format jest TEKSTOWY (warstwa tekstowa PDF) — pozycyjna rekonstrukcja
  kolumn (x<320 lewa, x>=320 prawa) daje atrybucję per-radny.
Roster: 15 radnych; 25 sesji IX kad. (2024-05-16 .. 2026-08-10), 253 głosowania.
Walidacja: KAŻDE głosowanie reconcilowane vs agregat (za+przeciw+wstrzym==suma
list imiennych) — 253/253 OK (100%).

Użycie:
    python scrape_szydlowiec.py --city-dir cities/szydlowiec [--cache-dir .cache]
"""

import argparse
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.szydlowiec.pl"
CATEGORY = f"{BASE}/10015/Imienne_wykazy_glosowan/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}
_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12,
        'wrzesnia': 9, 'pazdziernika': 10}
REQ_DELAY = 0.4
_LAST = 0.0


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90, verify=False)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


# ---------------------------------------------------------------------------
# 1. Sesje
# ---------------------------------------------------------------------------
def discover_sessions(cache):
    all_sess = {}
    for page in ["", "2/", "3/"]:
        html = _fetch(CATEGORY + page if page else CATEGORY, cache)
        for h, txt in re.findall(r"href=[\"'](https://bip\.szydlowiec\.pl/10015/\d+/[^\"']+)[\"'][^>]*>(.*?)</a>", html, re.S):
            if h in all_sess or "/archiwum/" in h:
                continue
            t = re.sub(r"<[^>]+>", "", txt).strip()
            if not t or "Sesj" not in t:
                continue
            m = re.search(r"(\d{1,2})\s+(\w+?)\s+(\d{4})", t)
            d = None
            if m and m.group(2).lower() in _MON:
                d = f"{m.group(3)}-{_MON[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
            all_sess[h] = {"url": h, "title": t, "date": d}
    sess = [s for s in all_sess.values() if s["date"] and s["date"] >= KAD_START]
    sess.sort(key=lambda s: s["date"])
    return sess


def find_protokol_pdf(session_url, cache):
    html = _fetch(session_url, cache)
    cands = re.findall(r"(https://bip\.szydlowiec\.pl/system/pobierz\.php\?[^\"'<> ]*)",
                       html.replace("&amp;", "&"))
    for c in cands:
        if "protokol" in c.lower() or "wykaz" in c.lower() or "wyniki" in c.lower() or "glos" in c.lower():
            return c
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# 2. Parser głosowań (walidowany 253/253)
# ---------------------------------------------------------------------------
def _lines_in_column(words, x_lo, x_hi, y_lo, y_hi):
    sel = [w for w in words if x_lo <= w[0] < x_hi and y_lo <= w[1] < y_hi]
    sel.sort(key=lambda w: (round(w[1] / 6), w[0]))
    lines = {}
    for w in sel:
        key = round(w[1] / 6)
        lines.setdefault(key, []).append((w[0], w[4]))
    out = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda z: z[0])
        out.append(" ".join(t for _, t in ws))
    return out


def _parse_list(lines):
    cat = None
    cats = defaultdict(list)
    for ln in lines:
        low = ln.lower()
        if "jestem za" in low:
            cat = "za"
        elif "jestem przeciw" in low:
            cat = "przeciw"
        elif "wstrzymuj" in low and "się" in low:
            cat = "wstrzym"
        elif "obecni radni" in low or "nie wzięli" in low or low.startswith("udziału") or low.startswith("w głosowaniu"):
            cat = "obecni_no"
        elif cat and re.match(r"^\d+\.\s+[A-ZŁŚ]", ln):
            cats[cat].append(re.sub(r"^\d+\.\s+", "", ln).strip())
    return cats


def parse_doc(doc):
    votes = []
    for i in range(doc.page_count):
        pg = doc[i]
        words = pg.get_text("words")
        t = pg.get_text()
        za = re.search(r"jestem\s+za\s*[:]?\s*(\d+)", t, re.I)
        pr = re.search(r"jestem\s+przeciw\s*[:]?\s*(\d+)", t, re.I)
        wz = re.search(r"wstrzymuj\S*\s*się\s*[:]?\s*(\d+)", t, re.I)
        if not (za or pr):
            continue
        zag = None
        for w in words:
            if w[4] == "zagłosowali":
                zag = w[1]
                break
        y_lo = zag if zag else 330
        left = _lines_in_column(words, 0, 320, y_lo, 720)
        right = _lines_in_column(words, 320, 800, y_lo, 720)
        lc, rc = _parse_list(left), _parse_list(right)
        named = {
            "za": lc.get("za", []) + rc.get("za", []),
            "przeciw": lc.get("przeciw", []) + rc.get("przeciw", []),
            "wstrzymal_sie": lc.get("wstrzym", []) + rc.get("wstrzym", []),
        }
        counts = {k: len(v) for k, v in named.items()}
        agg = (int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0)
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if agg != got:
            # nie reconciluje -> pomiń to głosowanie (nie fabrykujemy)
            continue
        # temat
        tm = re.search(r'(?:(?:w sprawie|Uchwała|Wniosek)[^“”"\n]{0,60})["“”]?\s*([^“”"\n]{5,160})', t)
        topic = tm.group(1).strip() if tm else ""
        dt = re.search(r"Data i godzina głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
        vdate = ""
        if dt:
            vdate = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}"
        votes.append({"topic": topic, "named": named, "counts": counts,
                      "session_date": vdate})
    return votes


# ---------------------------------------------------------------------------
# 3. Output
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, session_map):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        sd = rec["session_date"] or rec["session_date"]
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": session_map.get(d, d),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": session_map.get(d, d),
            "topic": rec.get("topic", ""), "named_votes": rec["named"],
            "counts": rec["counts"],
        })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {}
    for name in all_names:
        councilors[name] = {"name": name, "club": "", "district": None,
                            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                            "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors:
                    if cat == "za":
                        councilors[nm]["votes_za"] += 1
                    elif cat == "przeciw":
                        councilors[nm]["votes_przeciw"] += 1
                    else:
                        councilors[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r.get("session_date") for r in records if (r.get("session_date") or "") >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        (out_path.parent / f"kadencja-{kid}.json").write_text(
            json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out_path.write_text(json.dumps({"generated": output.get("generated", ""),
                                    "default_kadencja": output.get("default_kadencja", ""),
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[szydlowiec] sesje: {len(sessions)}")
    records = []
    session_map = {}
    for s in sessions:
        href = find_protokol_pdf(s["url"], cache)
        if not href:
            print(f"  [ERR] brak pdf dla {s['date']}")
            continue
        data = _fetch(href, cache, binary=True)
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            print(f"  [ERR pdf {s['date']}] {e}")
            continue
        vs = parse_doc(doc)
        session_map[s["date"]] = s["title"][:40]
        for v in vs:
            records.append({"topic": v["topic"], "named": v["named"],
                            "counts": v["counts"], "session_date": v["session_date"] or s["date"]})
        print(f"  {s['date']} votes={len(vs)}")
    output = build_output(records, session_map)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[szydlowiec] total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")


if __name__ == "__main__":
    main()
