#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Maków Mazowiecki — imienne głosowania Rady Miejskiej (DSSS Vote).

Źródło: https://bip.makowmazowiecki.pl  (BIP; system DSSS Vote, IX kadencja 2024-2029).
Kategoria /205,imienny-wykaz-glosowania-radnych-na-sesjach-rm zawiera 92 PDF-y
"imienny-wykaz-glosowania-radnych-rady-miejskiej-w-makowie-mazowieckim-na-XY-sesji-w-dniu-<data>.pdf"
(sesja+data w nazwie pliku). Każdy PDF to per-głosowanie załączniki DSSS Vote —
TEKSTOWA warstwa, nagłówki "Jestem za / Jestem przeciw / Wstrzymuję się /
Obecni radni, którzy nie wzięli udziału w głosowaniu", kolumny rekonstruowane
pozycyjnie (x<320 lewa, x>=320 prawa) jak w Szydłowcu.

Walidacja: KAŻDE głosowanie reconcilowane vs agregat (jestem za N, jestem przeciw M,
wstrzymuję się K == suma list imiennych). 24 sesje IX kad. (2024-05-07 .. 2026-06-25).

Użycie:
    python scrape_makow.py --city-dir cities/makow-mazowiecki [--cache-dir .cache]
"""

import argparse
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.makowmazowiecki.pl"
CATEGORY = f"{BASE}/205,imienny-wykaz-glosowania-radnych-na-sesjach-rm"
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


def _roman(s):
    r = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8,
         'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
         'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20, 'xxi': 21,
         'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25, 'xxvi': 26, 'xxvii': 27,
         'xxviii': 28, 'xxix': 29, 'xxx': 30}
    return r.get(s.lower())


def discover_pdfs(cache):
    """Enumerate imienny PDFs from the category page (all kadencje)."""
    html = _fetch(CATEGORY, cache)
    pat = re.findall(r"([^\"']*plik,\d+,imienny[^\"']*?\.pdf)", html.replace("&amp;", "&"))
    out = []
    for p in pat:
        url = p if p.startswith("http") else (BASE + "/" + p.lstrip("/"))
        m = re.search(r"-na-([ivx]+)-sesji-w-dniu-(\d+)-([a-ząćęłńóśźż]+)-(\d{4})", url, re.I)
        if not m:
            continue
        day = int(m.group(2))
        mon = _MON.get(m.group(3).lower())
        yr = int(m.group(4))
        if not mon:
            continue
        date = f"{yr}-{mon:02d}-{day:02d}"
        num = _roman(m.group(1))
        out.append({"url": url, "date": date, "number": num})
    # dedupe by url, ix kadencja only
    seen = set()
    res = []
    for r_ in out:
        if r_["url"] in seen:
            continue
        seen.add(r_["url"])
        if r_["date"] >= KAD_START:
            res.append(r_)
    res.sort(key=lambda x: x["date"])
    return res


# --- DSSS Vote parser (identyczny jak Szydłowiec, walidowany 253/253) -------
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


def parse_votes_from_text(per_vote_blocks):
    """Per-vote blocks split from pdfplumber text (makow PDF: one uchwała per page)."""
    votes = []
    for t in per_vote_blocks:
        za = re.search(r"jestem\s+za\s*[:]?\s*(\d+)", t, re.I)
        pr = re.search(r"jestem\s+przeciw\s*[:]?\s*(\d+)", t, re.I)
        wz = re.search(r"wstrzymuj\S*\s*się\s*[:]?\s*(\d+)", t, re.I)
        if not (za or pr):
            continue
        # named list parse from lines
        lines = [l for l in t.split("\n") if l.strip()]
        cats = _parse_list(lines)
        named = {
            "za": cats.get("za", []),
            "przeciw": cats.get("przeciw", []),
            "wstrzymal_sie": cats.get("wstrzym", []),
        }
        counts = {k: len(v) for k, v in named.items()}
        agg = (int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0)
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if agg != got:
            continue  # nie fabrykujemy nie-reconcilujących
        tm = re.search(r'(?:(?:w sprawie|Uchwała|Wniosek)[^“”“”"\n]{0,60})["“”"]?\s*([^“”"\n]{5,160})', t)
        topic = tm.group(1).strip() if tm else ""
        dt = re.search(r"Data i godzina głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
        vdate = ""
        if dt:
            vdate = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}"
        votes.append({"topic": topic, "named": named, "counts": counts, "session_date": vdate})
    return votes


def parse_doc_text(page_text):
    """Split doc text into per-vote blocks (one uchwała per page in these PDFs)."""
    blocks = page_text.split("\n\n")
    return blocks


# --- output builders (identyczne jak Szydłowiec) ---------------------------
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

    pdfs = discover_pdfs(cache)
    print(f"[makow] sesje/PDF-y IX kadencji: {len(pdfs)}")
    records = []
    session_map = {}
    for p in pdfs:
        try:
            data = _fetch(p["url"], cache, binary=True)
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            print(f"  [ERR pdf {p['date']}] {e}")
            continue
        votes = []
        for i in range(doc.page_count):
            t = doc[i].get_text()
            # each page = one uchwała vote; use positional parser on page words
            words = doc[i].get_text("words")
            zag = None
            for w in words:
                if w[4] == "zagłosowali":
                    zag = w[1]
                    break
            y_lo = zag if zag else 330
            left = [l for l in _lines_in_column(words, 0, 320, y_lo, 720) if l.strip()]
            right = [l for l in _lines_in_column(words, 320, 800, y_lo, 720) if l.strip()]
            lc, rc = _parse_list(left), _parse_list(right)
            za = re.search(r"jestem\s+za\s*[:]?\s*(\d+)", t, re.I)
            pr = re.search(r"jestem\s+przeciw\s*[:]?\s*(\d+)", t, re.I)
            wz = re.search(r"wstrzymuj\S*\s*się\s*[:]?\s*(\d+)", t, re.I)
            if not (za or pr):
                continue
            named = {
                "za": lc.get("za", []) + rc.get("za", []),
                "przeciw": lc.get("przeciw", []) + rc.get("przeciw", []),
                "wstrzymal_sie": lc.get("wstrzym", []) + rc.get("wstrzym", []),
            }
            counts = {k: len(v) for k, v in named.items()}
            agg = (int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0)
            got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
            if agg != got:
                continue
            tm = re.search(r'(?:(?:w sprawie|Uchwała|Wniosek)[^“”"\n]{0,60})["“”"]?\s*([^“”"\n]{5,160})', t)
            votes.append({"topic": tm.group(1).strip() if tm else "",
                          "named": named, "counts": counts, "session_date": p["date"]})
        session_map[p["date"]] = f"Sesja {p['number']}"
        for v in votes:
            records.append(v)
        print(f"  {p['date']} (sesja {p['number']}) votes={len(votes)}")
    output = build_output(records, session_map)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[makow] total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")


if __name__ == "__main__":
    main()
