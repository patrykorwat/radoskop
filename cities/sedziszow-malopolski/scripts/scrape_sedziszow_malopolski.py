#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Sędziszów Małopolski — imienne głosowania Rady Miejskiej w Sędziszowie
Małopolskim (IX kadencja 2024-2029).

Źródło: BIP bip.sedziszow.pl (SYSTEMDOBIP.PL, E-LINE), kategoria
"Rada Miejska Sędziszów -> Protokoły głosowań -> Kadencja Rady 2024 - 2029"
(/10176 -> /10243), z podkategoriami rocznymi 2026 (/10317) i 2025 (/10244).

Dla KAŻDEJ sesji publikowany jest załącznik PDF "Protokół z głosowania - sesja NN -
ROK.pdf" (link system/pobierz.php?plik=...&id=...). Format tekstowy (bez OCR):

    <NN> <n>. Głosowanie w sprawie <temat>
    GŁOSOWAŁO: <K>
    głosowało ZA: <a>
    głosowało PRZECIW: <b>
    WSTRZYMAŁO się: <c>
    LP. Nazwisko i Imię jak głosował
    1 ADAMSKI Paweł głosował ZA
    2 KALITA Damian nie głosował
    ... (WSTRZYMAŁ się / głosował PRZECIW)

Krycie: sesje XV..XXXI IX kadencji (2025-04-09 .. 2026-08-19). Sesja XX/2025 nie ma
opublikowanego protokołu głosowań (brak załącznika); sesje 2024 (I..XIV) również nie
mają protokołów głosowań w tej kategorii (tylko pod rokiem 2025/2026). Roster = 15
radnych z "Skład Rady Miejskiej" (/10172). Kluby niepublikowane w BIP -> NZ (PENDING).
Walidacja per głos: suma głosów imiennych == liczniki z nagłówka (GŁOSOWAŁO / ZA /
PRZECIW / WSTRZYMAŁO), a liczba "nie głosował" == GŁOSOWAŁO - (ZA+PRZECIW+WSTRZYMAŁO).

Użycie:
    python scrape_sedziszow_malopolski.py --city-dir <cities/sedziszow-malopolski> \
        [--work-dir dir] [--cache-dir dir]
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

BIP = "https://bip.sedziszow.pl"
VOTES_CATS = ["10317", "10244"]  # 2026, 2025
SKLAD_URL = f"{BIP}/10172/Sklad_Rady_Miejskiej/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.5
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir, binary=True):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                (cache_dir / (key + ".dat")).write_bytes(data)
            return data
        except (ConnectionError, Timeout, OSError) as e:
            if attempt == 5:
                raise
            time.sleep(3 + attempt * 4)
    raise RuntimeError(f"GET failed: {url}")


# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """Scan the votes year-categories for session vote-protocol PDF links.
    Returns list of {roman, num, date, votes_url, fname} sorted by date."""
    sessions = []
    for cat in VOTES_CATS:
        t = _get(f"{BIP}/{cat}/", cache_dir).decode("utf-8", "ignore")
        for m in re.finditer(r'href="([^"]*system/pobierz\.php[^"]*)"', t):
            href = unescape(m.group(1))
            q = {}
            for kv in href.split("?")[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = v
            fname = q.get("plik", "")
            fid = q.get("id", "")
            rm = re.search(r'sesja_([IVXLCDM]+)[-_]', fname, re.I)
            roman = rm.group(1).upper() if rm else ""
            if not roman:
                continue
            # date from session title: fetch article page and parse "w dniu" / "dn.:"
            url = f"{BIP}/system/pobierz.php?plik={q.get('plik')}&id={fid}"
            sessions.append({"roman": roman, "votes_url": url, "fname": fname})
    # dedupe by roman
    seen = {}
    for s in sessions:
        key = s["roman"]
        seen[key] = s
    sessions = list(seen.values())
    # determine dates from the PDF header instead: we parse per-PDF. Assign num/date here
    sessions.sort(key=lambda s: _roman_int(s["roman"]))
    return sessions


_ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}


def _roman_int(s):
    tot = 0; prev = 0
    for ch in reversed(s.upper()):
        v = _ROMAN.get(ch, 0)
        if v < prev:
            tot -= v
        else:
            tot += v; prev = v
    return tot


def parse_date_from_pdf(text):
    """Extract session date from PDF header: 'Sędziszów, dn.: 19 sierpnia 2026 roku'
    or '... w dniu 19 sierpnia 2026 roku'."""
    MONTHS = {1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia", 5: "maja",
              6: "czerwca", 7: "lipca", 8: "sierpnia", 9: "września",
              10: "października", 11: "listopada", 12: "grudnia"}
    for pat in (r'dn\.:?\s*(\d{1,2})\s+(\w+)\s+(\d{4})',
                r'w dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})'):
        m = re.search(pat, text)
        if m:
            day, monstr, year = m.group(1), m.group(2), m.group(3)
            for k, v in MONTHS.items():
                if monstr.lower() == v:
                    return f"{year}-{k:02d}-{int(day):02d}"
    return None


# ---------------- parsing ----------------
_START_RE = re.compile(r'^\s*\d{1,3}\s+\d{1,3}\.\s*Głosowanie')
_START_RE2 = re.compile(r'^\s*\d{1,3}\.\s*Głosowanie')
_AGG_RE = {
    "GŁOSOWAŁO": re.compile(r'GŁOSOWAŁO:\s*(\d+)'),
    "za": re.compile(r'głosowało ZA:\s*(\d+)'),
    "przeciw": re.compile(r'głosowało PRZECIW:\s*(\d+)'),
    "wstrzymalo": re.compile(r'WSTRZYMAŁO się:\s*(\d+)'),
}
_ROW_RE = re.compile(
    r'^(\d{1,2})\s+([A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ-]+)\s+'
    r'([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:[- ][A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*)\s+'
    r'(głosował ZA|głosował PRZECIW|WSTRZYMAŁ się|nie głosował|głosowała ZA|głosowała PRZECIW|WSTRZYMAŁA się)'
)


def parse_vote_pdf(data):
    """Parse one 'Protokół z głosowania' PDF -> session metadata + list of
    {topic, named:{za,przeciw,wstrzymal_sie,brak}}, validated. Returns (date, records, n_fail)."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return None, [], 1
    date = parse_date_from_pdf(text)
    lines = [l for l in text.split("\n")]
    records = []
    n_fail = 0
    i = 0
    nlines = len(lines)
    while i < nlines:
        l = lines[i]
        # vote start: "N N. Głosowanie w sprawie ..." (or "N N.Głosowanie")
        m = _START_RE.match(l) or _START_RE2.match(l)
        if not m:
            i += 1
            continue
        # collect topic lines (until aggregate)
        j = i
        topic_parts = []
        while j < nlines and 'GŁOSOWAŁO' not in lines[j]:
            topic_parts.append(lines[j].strip())
            j += 1
        # aggregates
        agg = {}
        while j < nlines and 'Nazwisko' not in lines[j] and 'LP' not in lines[j]:
            for key, pat in _AGG_RE.items():
                mm = pat.search(lines[j])
                if mm:
                    agg[key] = int(mm.group(1))
            # stop if we hit a row line or footer
            if re.match(r'^\s*\d{1,2}\s+[A-ZĄĆĘŁŃÓŚŹŻ]', lines[j]) and 'głosował' in lines[j]:
                break
            if lines[j].strip().startswith('Sędziszów, dn.'):
                break
            j += 1
        # skip LP header if present
        if j < nlines and ('Nazwisko' in lines[j] or lines[j].strip().startswith('LP')):
            j += 1
        # rows
        rows = []
        while j < nlines:
            lj = lines[j].strip()
            rm = _ROW_RE.match(lj)
            if not rm:
                # next vote / footer
                if _START_RE.match(lj) or _START_RE2.match(lj) or lj.startswith('Sędziszów, dn.'):
                    break
                j += 1
                continue
            rows.append((rm.group(2), rm.group(3), rm.group(4)))
            j += 1
        # map rows to categories
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": []}
        for sur, first, vote in rows:
            nm = f"{sur} {first}"
            v = vote.replace("głosował ", "").replace("głosowała ", "").lower()
            if v.startswith("za"):
                named["za"].append(nm)
            elif v.startswith("przeciw"):
                named["przeciw"].append(nm)
            elif v.startswith("wstrzymał") or v.startswith("wstrzymala"):
                named["wstrzymal_sie"].append(nm)
            else:  # nie głosował
                named["brak"].append(nm)
        # validate against aggregates
        ok = True
        if "glosowalo" in agg and "za" in agg:
            present = len(named["za"]) + len(named["przeciw"]) + len(named["wstrzymal_sie"])
            if present != agg["glosowalo"]:
                ok = False
            if named["za"] and len(named["za"]) != agg["za"]:
                ok = False
            if "przeciw" in agg and len(named["przeciw"]) != agg["przeciw"]:
                ok = False
            if "wstrzymalo" in agg and len(named["wstrzymal_sie"]) != agg["wstrzymalo"]:
                ok = False
        topic = " ".join(x.strip() for x in topic_parts) if topic_parts else ""
        topic = re.sub(r'^\s*\d{1,3}\s+\d{1,3}\.\s*', '', topic).strip()
        if not ok:
            n_fail += 1
        records.append({"topic": topic, "named": named, "validated": ok})
        i = j
    return date, records, n_fail


# ---------------- name normalization ----------------
def norm_pl_name(s):
    """Normalize a 'Nazwisko Imię' -> look up. We store names from roster as 'Imię Nazwisko'."""
    return re.sub(r'\s+', ' ', s).strip()


def _make_roster(roster):
    """roster = set/list of 'Imię Nazwisko'. Build map from 'NAZWISKO Imię' (per PDF order)
    to canonical. We rely on the fact PDF lists 'NAZWISKO Imię'."""

    def mkkey(s):
        return re.sub(r'\s+', ' ', s).strip().lower()

    canon_rev = {}
    for nm in roster:
        toks = nm.split()
        if len(toks) >= 2:
            rev = f"{toks[-1]} {toks[0]}".lower()  # NAZWISKO imię
            canon_rev[rev] = nm
        elif toks:
            canon_rev[toks[0].lower()] = nm
    return canon_rev


def map_to_roster(named, canon_rev):
    """Convert named dict lists from 'NAZWISKO Imię' format to canonical 'Imię Nazwisko'
    using canon_rev. Unmatched names kept as-is (uppercased surname)."""
    out = {k: [] for k in named}
    for cat, names in named.items():
        for nm in names:
            key = nm.lower()
            out[cat].append(canon_rev.get(key, nm))
    return out


# ---------------- output ----------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's',
            'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
            'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
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
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "nieobecni":
                    councilors_data[nm]["votes_nieobecny"] += 1
                elif cat == "brak":
                    councilors_data[nm]["votes_brak"] += 1
                elif cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
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
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
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
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8")) if (city_dir / "config.json").is_file() else {}
    club_assign = cfg.get("club_assignments", {}) or {}
    roster = set(cfg.get("councilor_roster", []))
    canon_rev = _make_roster(roster)

    sessions = discover_sessions(cache)
    print(f"[sedziszow] {len(sessions)} sesji z protokołem głosowań")

    records = []
    n_fail_total = 0
    n_validated = 0
    n_total = 0
    for se in sessions:
        url = se["votes_url"]
        data = _get(url, cache)
        fname = se["fname"]
        pdf_path = pdf_dir / f"{se['roman']}.pdf"
        pdf_path.write_bytes(data)
        date, recs, n_fail = parse_vote_pdf(data)
        n_fail_total += n_fail
        if date is None:
            print(f"  [!] {se['roman']} brak daty (n={len(recs)} glosowan, fail={n_fail})")
        for r in recs:
            r["date"] = date
            r["num"] = _roman_int(se["roman"])
            r["named"] = map_to_roster(r["named"], canon_rev)
            if r.get("validated"):
                n_validated += 1
            n_total += 1
        records += recs
        print(f"  [ok] {se['roman']} date={date} votes={len(recs)} fail={n_fail}")
    print(f"[sedziszow] total votes={len(records)} validated={n_validated}/{n_total} sheetfail={n_fail_total}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign, roster)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[sedziszow] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
