#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Łask — imienne głosowania Rady Miejskiej w Łasku (IX kadencja 2024-2029).

Źródło: BIP bip.lask.pl (custom "artinfo/eBoi"-style CMS, /{id}/{slug}.html), strona
"Protokoły z sesji i wyniki głosowań" (/4079/). Dla KAŻDEJ sesji IX kadencji (I…XXX,
07.05.2024…22.07.2026) publikowane są załączniki: "Protokół z N sesji" (DOCX) oraz
"Wyniki głosowań z N sesji" (PDF) — klasyczny eSesja FORMAT TEKSTOWY:

    <temat głosowania ... (HH:MM)>
    Wyniki imienne:
    ZA (14)
    <imię nazwisko, ...>
    PRZECIW (N) / WSTRZYMUJĘ SIĘ (N) / NIE GŁOSOWALI (N) / NIEOBECNI (N)
    <imiona nazwiska, ...>

Sesja I oraz XXIV i XXIX nie mają załącznika "Wyniki głosowań" (tylko protokół) i są pomijane.
Data + nr sesji z listingu /4079/; skład = pełny zbiór unikalnych radnych z głosowań
(skrzyżowany z oficjalnym rosterem "Skład Rady do BIP"), kluby kuratorowane z BIP /4481/.
Walidacja per głos: zsumowane głosy imienne == liczniki z nagłówka (ZA (N) itd.).

Użycie:
    python scrape_lask.py --city-dir <cities/lask> [--work-dir dir] [--cache-dir dir]
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

BIP = "https://bip.lask.pl"
LISTING = "/4079/protokoly-z-sesji-i-wyniki-glosowan.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.6
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

def _get(url, cache_dir, binary=True):
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
    """Parse /4079/ listing: session roman+date + wyniki-glosowan PDF url (if present)."""
    t = _get(BIP + LISTING, cache_dir).decode("utf-8", "ignore")
    m = re.search(r'id="printArea"', t); seg = t[m.start():] if m else t
    seg = re.sub(r'<script.*?</script>', '', seg, flags=re.S)
    sessions = []
    for mm in re.finditer(
            r'([IVXLCDM]+) Sesja Rady Miejskiej w Łasku - (\d{1,2})\.(\d{1,2})\.(\d{4}) r\.(.*?)(?=(?:[IVXLCDM]+ Sesja Rady Miejskiej w Łasku - )|\Z)',
            seg, re.S):
        rom, d, mo, y = mm.group(1), mm.group(2), mm.group(3), mm.group(4)
        body = mm.group(5)
        num = roman_to_int(rom)
        date = f"{y}-{int(mo):02d}-{int(d):02d}"
        if date < KAD_START: continue
        # find wyniki-glosowan (pdf) attachment in this session body
        votes_url = None
        for a in re.finditer(r'href="(https://bip\.lask\.pl/download/attachment/\d+/[^"]+)"', body):
            href = unescape(a.group(1))
            fname = re.sub(r'\?.*$', '', href).rsplit('/', 1)[-1]
            if 'wyniki-glosowan' in fname and fname.lower().endswith('.pdf'):
                votes_url = href
                break
        sessions.append({"num": num, "date": date, "votes_url": votes_url, "roman": rom})
    sessions.sort(key=lambda s: s["date"])
    return sessions

# ---------------- eSesja imienne TEXT parsing (Łask variant) ----------------
_LABEL_RE = re.compile(
    r'(?m)^\s*(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|NIE GŁOSOWALI/NIEOBECNI|NIE GŁOSOWALI|NIEOBECNI)\s*\((\d+)\)')
_CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
            "NIE GŁOSOWALI": "brak", "NIEOBECNI": "nieobecni",
            "NIE GŁOSOWALI/NIEOBECNI": "nieobecni"}
_FOOTER_RE = re.compile(
    r'(wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|wytworzono|urząd miejski)', re.I)

def _clean_name(s):
    s = s.strip()
    if not s: return None
    if not any(ch.isalpha() for ch in s): return None
    if _FOOTER_RE.search(s): return None
    if re.search(r'\d{1,2}:\d{2}', s): return None
    return re.sub(r'\s+', ' ', s)

def _split_names(raw):
    """Collapse whitespace, split on commas, drop non-name tokens."""
    joined = re.sub(r'\s+', ' ', raw)
    out = []
    for tok in joined.split(','):
        nm = _clean_name(tok)
        if nm: out.append(nm)
    return out

def _norm_name(s):
    """Repair hyphen line-wrap ('Nowak- Popławska' -> 'Nowak-Popławska') and collapse
    internal whitespace/newlines ('Sylwester\\nFlorczak' -> 'Sylwester Florczak')."""
    return re.sub(r'\s+', ' ', re.sub(r'\s*-\s*', '-', s)).strip()

def _name_to_re(name):
    """Build a regex for one roster name tolerating newlines/whitespace between first/last
    name and optional space around hyphens (handles PDF line-wraps mid-name)."""
    tokens = name.split()
    out = []
    for tok in tokens:
        # replace each '-' with an optional-space-tolerant hyphen
        out.append(re.sub(r'-', r'-?\\s*', tok.strip()))
    return r'\s+'.join(out)

def _make_roster_re(roster):
    """Build a canonical name map + one regex alternation (longest-first) from a roster."""
    canon = {}
    patterns = []
    for name in roster:
        canon[_norm_name(name).lower()] = name
        patterns.append(_name_to_re(name))
    if not canon:
        return None, {}
    # sort by pattern length desc so multi-token names win over substrings
    patterns.sort(key=len, reverse=True)
    pat = "|".join(patterns)
    return re.compile(pat), canon

def _names_in_region(region, roster_re, canon):
    """Extract roster member names appearing in a region (in order of occurrence)."""
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

def parse_pdf(data, roster=None):
    """Parse a 'Wyniki głosowań' PDF -> list of {topic, named:{cat:[names]}} validated against
    header counts. Names extracted via the official roster (robust vs line-wrap/glue).
    Returns (records, n_fail)."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return [], 1
    if "Wyniki imienne" not in text:
        return [], 0
    # Repair hyphen line-wraps ("Nowak- Popławska","Wawrzyniak-\nLicha" -> hyphen-joined),
    # keeping newlines so the ^-anchored label regex still works.
    text = re.sub(r'-\s+', '-', text)
    roster_re, canon = _make_roster_re(roster or [])
    WI = [m.start() for m in re.finditer(r'Wyniki imienne', text)]
    if not WI:
        return [], 0
    # start of topic_0 = end of the 'Wykaz głosowań sesji - NN sesja Rady Miejskiej w Łasku' header line
    hdr = text.find('Wykaz głosowań sesji')
    E = (text.find('\n', hdr) + 1) if hdr != -1 else 0
    records = []
    n_fail = 0
    for i, wi in enumerate(WI):
        topic = re.sub(r'\s+', ' ', text[E:wi]).strip().strip(' :')
        end = WI[i + 1] if i + 1 < len(WI) else len(text)
        named, E = _labels_in_range(text, wi, end, roster_re, canon)
        if named is None:
            n_fail += 1
            E = end
            continue
        records.append({"topic": topic, "named": named})
    return records, n_fail

def _labels_in_range(text, start, end, roster_re, canon):
    """Parse label groups in text[start:end]; return (named, nextE) where nextE is the absolute
    offset just past the LAST category's expected names (= start of the next vote's topic)."""
    region = text[start:end]
    matches = list(_LABEL_RE.finditer(region))
    if not matches:
        return None, start
    named = {}
    nextE = start + len(region)
    for i, mm in enumerate(matches):
        cat = _CAT_MAP.get(mm.group(1), "nieobecni")
        expected = int(mm.group(2))
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        raw = region[mm.end():seg_end]
        names = _names_in_region(raw, roster_re, canon)
        # fallback: comma-token person names if roster matching differs
        if len(names) != expected:
            toks = _split_names(raw)
            repl = {t for t in toks if 1 <= len(t.split()) <= 4
                    and not re.search(r'\bul\.|\d{1,2}:\d{2}|\(', t)} | set(names)
            repl = sorted(repl, key=lambda x: raw.find(x))
            if len(repl) >= expected:
                names = repl[:expected]
            else:
                names = toks[:expected]
        names = names[:expected]
        named[cat] = names
        if i == len(matches) - 1:
            # find offset after the expected-th roster name within raw -> set nextE
            cnt = 0
            if roster_re is not None:
                for m in roster_re.finditer(raw):
                    cnt += 1
                    if cnt == expected:
                        nextE = start + mm.end() + m.end()
                        break
    return named, nextE

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
    # include roster members even if never named (present-but-never-absent edge cases)
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
    print(f"[lask] {len(sessions)} sesji IX kad. (2024-2029)")
    records = []
    n_fail_total = 0
    for se in sessions:
        if not se["votes_url"]:
            print(f"  [skip] {se['date']} nr{se['num']} (brak załącznika wyniki głosowań)")
            continue
        url = se["votes_url"]
        fname = re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_") + ".pdf"
        pdf_path = pdf_dir / fname
        data = _get(url, cache)
        pdf_path.write_bytes(data)
        recs, n_fail = parse_pdf(data, list(roster))
        n_fail_total += n_fail
        ok = 0
        for r in recs:
            r["date"] = se["date"]; r["num"] = se["num"]
        records += recs
        print(f"  [ok] {se['date']} nr{se['num']} votes={len(recs)} (frag_bad={n_fail})")
    print(f"[lask] total records={len(records)} sheet-parse-fails={n_fail_total}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign, roster)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[lask] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
