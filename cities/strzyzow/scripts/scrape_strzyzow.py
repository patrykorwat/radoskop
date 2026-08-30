#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Strzyżów — imienne głosowania Rady Miejskiej (skanowany DSSS print, OCR).

Źródło: http://bip.strzyzow.pl  (BIP; eBOI/Softres; IX kadencja 2024-2029).
Rada publikuje per-sesja PDF "Wyniki głosowania z {N} sesji ..." w kategorii
"Protokoły" (under=11&grp=17, deps 200-245). PDF jest SKANOWANY (brak warstwy
tekstowej) — format wydruku "PROTOKÓŁ GŁOSOWANIA z dnia <data>": każda strona =
jedno głosowanie jawne (Punkt porządku obrad + Przedmiot + agregat TAK/NIE/WSTRZ
+ lista imienna "Głosy oddane: NAME TAK/NIE/WSTRZ..."). Ekstrakcja: PyMuPDF
render dpi=150 + tesseract -l pol; atrybucja per-radny z listy imiennej;
KAŻDE głosowanie reconcilowane vs agregat (liczba oddanych głosów == suma list
imiennej); niespójne są pomijane (nie fabrykujemy).

Użycie:
    python scrape_strzyzow.py --city-dir cities/strzyzow [--cache-dir .cache]
"""
import argparse
import hashlib
import json
import re
import subprocess
import time
import os
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "http://bip.strzyzow.pl"
PROTO_CAT = f"{BASE}/index.php?page=zwykly.php&under=11&grp=17&dep="
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}
REQ_DELAY = 0.4
_LAST = 0.0
TESSERACT = os.environ.get("RADOSKOP_TESSERACT", "tesseract")
DPI = int(os.environ.get("RADOSKOP_OCR_DPI", "150"))
_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'wrzesnia': 9, 'pazdziernika': 10,
        'luty': 2, 'stycznia': 1}
ROMAN = ['I','II','III','IV','V','VI','VII','VIII','IX','X','XI','XII','XIII',
         'XIV','XV','XVI','XVII','XVIII','XIX','XX','XXI','XXII','XXIII','XXIV',
         'XXV','XXVI','XXVII','XXVIII']

def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()

def _fetch(url, cache=None, binary=False):
    ck = hashlib.md5(url.encode()).hexdigest() if cache else None
    if cache:
        cf = Path(cache) / (ck + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (ck + (".bin" if binary else ".html"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


# ---------------------------------------------------------------------------
# 1. Metadane: enuemry dep 200-245, zbierz per-sesja "Wyniki głosowania" PDF
# ---------------------------------------------------------------------------
def discover_sessions(cache):
    sess = {}
    for dep in range(200, 246):
        try:
            html = _fetch(PROTO_CAT + str(dep), cache)
        except Exception:
            continue
        for m in re.finditer(
                r'name="attach_file_path"\s+value="([^"]+)"\s*>\s*<button[^>]*>(.*?)</button>',
                html, re.S):
            path = m.group(1)
            lbl = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if "Wyniki g" not in lbl or "Uchwa" in lbl:
                continue
            rm = re.search(r"z\s+([IVXLC]+)\s+sesji", lbl, re.I)
            if not rm:
                continue
            rom = rm.group(1).upper()
            if rom not in ROMAN or rom in sess:
                continue
            sess[rom] = {"pdf": path, "label": lbl[:90]}
    ordered = {r: sess[r] for r in ROMAN if r in sess}
    return ordered


# ---------------------------------------------------------------------------
# 2. OCR pojedynczej strony -> tekst
# ---------------------------------------------------------------------------
def _ocr_pdf(doc, page_i, cache_key=None, ocr_cache=None):
    if ocr_cache is not None and cache_key:
        cf = Path(ocr_cache) / cache_key / f"{page_i}.txt"
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    pix = doc[page_i].get_pixmap(dpi=DPI)
    tmp = Path(f"/tmp/_strz_ocr_{os.getpid()}_{page_i}.png")
    pix.save(tmp)
    try:
        proc = subprocess.run([TESSERACT, str(tmp), "-", "-l", "pol", "--psm", "3"],
                              capture_output=True, timeout=120)
        out = proc.stdout.decode("utf-8", errors="ignore")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if ocr_cache is not None and cache_key:
        (Path(ocr_cache) / cache_key).mkdir(parents=True, exist_ok=True)
        (Path(ocr_cache) / cache_key / f"{page_i}.txt").write_text(out, encoding="utf-8")
    return out


_VOTE_TOKENS = re.compile(r"\b(TAK|NIE|WSTRZ[YU]M[IU]?J[EĄĘS]?\w*|WSTRZ)\b", re.I)

def _norm_name(name):
    return re.sub(r"\s+", " ", name).strip(" .-[]()\t|")


def _parse_page_text(t):
    """Extract one vote's aggregate + per-councillor list from OCR text of one page."""
    # session date
    dm = re.search(r"z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", t, re.I)
    vdate = ""
    if dm:
        mon = _MON.get(dm.group(2).lower())
        if mon:
            vdate = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
    # oddane głosy
    og = re.search(r"Liczba\s+oddanych\s+głos[oó]w\s*:\s*(\d+)", t, re.I)
    oddane = int(og.group(1)) if og else 0
    # per-councillor list after "Głosy oddane:"
    named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
    gl_text = ""
    gi = re.search(r"Głosy\s+oddane\s*:", t, re.I)
    if gi:
        gl_text = t[gi.end():]
        # stop at footer noise (page border label), otherwise the list runs to EOF
        k = gl_text.find("Ust>Konf")
        if k != -1:
            gl_text = gl_text[:k]
    for line in gl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"^(.*?)\s+(TAK|NIE|WSTRZ[YU]M\w*|WSTRZ)\s*$", line, re.I)
        if not m:
            continue
        name = _norm_name(m.group(1))
        vote = m.group(2).upper()
        if not name or len(name) < 4:
            continue
        if vote == "TAK":
            named["za"].append(name)
        elif vote == "NIE":
            named["przeciw"].append(name)
        else:
            named["wstrzymal_sie"].append(name)
    # subject (best-effort): the row after 'porządku obrad' header with point number + counts
    topic = _extract_topic(t)
    return {"topic": topic, "session_date": vdate, "oddane": oddane, "named": named}


def _extract_topic(t):
    # find "obrad" heading region; collect subject lines until Rezultat
    m = re.search(r"obrad\s*(\d+)\s+(.*?)(?:Rezultat\s+głosowania|Rezultat\s+glosowania)", t, re.S | re.I)
    if not m:
        return ""
    subj = m.group(2)
    subj = re.sub(r"\s*\d{1,3}\s+\d{1,3}\s+\d{1,3}\s*", " ", subj)
    subj = re.sub(r"\s+", " ", subj).strip(" \t|[]")
    return subj[:200]


def parse_session_pdf(pdf_bytes, ocr_cache=None):
    """Return (records, session_date, ok_votes, total_pages)."""
    import hashlib as _h
    pdf_key = _h.md5(pdf_bytes).hexdigest()[:12]
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    records = []
    sdate = ""
    for i in range(doc.page_count):
        t = _ocr_pdf(doc, i, cache_key=pdf_key, ocr_cache=ocr_cache)
        v = _parse_page_text(t)
        if not v["named"]["za"] and not v["named"]["przeciw"] and not v["named"]["wstrzymal_sie"]:
            continue  # no parsable per-councillor list (blank/noise page)
        # reconcile: za+nier+wstrz == oddane (>0)
        got = len(v["named"]["za"]) + len(v["named"]["przeciw"]) + len(v["named"]["wstrzymal_sie"])
        if v["oddane"] > 0 and got == v["oddane"]:
            if not sdate and v["session_date"]:
                sdate = v["session_date"]
            records.append(v)
    doc.close()
    return records, sdate


# ---------------------------------------------------------------------------
# 3. Output (reuse szydlowiec-style builder)
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
            "counts": {k: len(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
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


def build_profiles(records, session_map):
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


def _norm_for_match(name):
    """Uppercase, strip non-letter junk (leading /, ), [, etc.), collapse spaces."""
    n = name.upper()
    n = re.sub(r"[^A-ZŁŚŹŻĆŃĘÓĄ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _canonicalize(records, min_ratio=0.84):
    """Merge OCR name variants into canonical councilor names (fuzzy dedup).

    OCR noise creates spelling variants of the same councillor (leading junk
    chars, doubled/missing letters, missing diacritics). We cluster variants by
    SequenceMatcher similarity (on a normalized, junk-stripped form) against the
    most frequent spelling, which becomes the canonical name. Returned records
    use canonical names; per-vote cardinality is unchanged, so reconciliation
    still holds.
    """
    from collections import Counter
    import difflib
    cnt = Counter()
    for r in records:
        for n in list(r["named"]["za"]) + r["named"]["przeciw"] + r["named"]["wstrzymal_sie"]:
            cnt[n] += 1
    canon = {}
    nmap = {}
    for name, _f in cnt.most_common():
        norm = _norm_for_match(name)
        best, br = None, 0.0
        for c in canon:
            ratio = difflib.SequenceMatcher(None, norm, canon[c]).ratio()
            if ratio > br:
                br, best = ratio, c
        if best and br >= min_ratio:
            canon[name] = canon[best]
        else:
            canon[name] = norm
        nmap[name] = canon[name]
    for r in records:
        for k in ("za", "przeciw", "wstrzymal_sie"):
            r["named"][k] = [nmap.get(n, n) for n in r["named"][k]]
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[strzyzow] sesje: {len(sessions)} ({', '.join(sessions.keys())})")
    ocr_cache = (cache / "ocr") if cache else None
    records = []
    session_map = {}
    skipped = {}
    for rom, s in sessions.items():
        try:
            data = _fetch(s["pdf"], cache, binary=True)
            vs, sdate = parse_session_pdf(data, ocr_cache=ocr_cache)
        except Exception as e:
            print(f"  [ERR {rom}] {e}")
            skipped[rom] = f"err:{e}"
            continue
        if not sdate:
            skipped[rom] = "no-date/votes"
            continue
        session_map[sdate] = s["label"][:40]
        for v in vs:
            v["session_date"] = sdate
            records.append(v)
        print(f"  {rom} ({sdate}) votes={len(vs)} reconciled")
    records = _canonicalize(records)
    output = build_output(records, session_map)
    profiles = build_profiles(records, session_map)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[strzyzow] RESULT total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']} skipped={skipped}")


if __name__ == "__main__":
    main()
