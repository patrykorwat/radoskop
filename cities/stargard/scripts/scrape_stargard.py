#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Stargard — imienne głosowania Rady Miejskiej w Stargardzie (IX kadencja).

Źródło: BIP bip.stargard.eu (custom CMS "SSDIP"). Drzewo „Rada Miejska →
Uchwały Rady Miejskiej → Kadencja 2024-2029" (kategoria /22474) zawiera stronę
każdej sesji (href /{id}, tytuł „{ROMAN} sesja Rady Miejskiej w dniu:{data}").
Na stronie sesji: dokumenty „{sid}/dokument/{did}" — każda uchwała ma załącznik
„Głosowanie.pdf" (1 głosowanie), osobny dokument „Pozostałe głosowania" zawiera
plik „{SESM}, Głosowanie {N}, Data {date}.pdf" na każde głosowanie proceduralne.
Pliki przez api/download/file?id=.

PDF = jednostronicowy wydruk: nagłówek „NN {ROMAN} Sesja ... / Głosowanie /
<temat> / Typ głosowania jawne Data głosowania: DD.MM.YYYY HH:MM / Liczba
uprawnionych N Głosy za N / ... / Uprawnieni do głosowania" + dwukolumnowa
tabela imienna „N. Imię Nazwisko <ZA|PRZECIW|WSTRZYMUJĘ SIĘ|NIEOBECNA> N. ..."
Walidacja: sumy imienne == agregaty „Głosy za/przeciw/wstrzymujące się".

Użycie:
    python scrape_stargard.py --city-dir <cities/stargard> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
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

BIP = "https://bip.stargard.eu"
KAD_CAT = "/22474"  # Uchwały RM / Kadencja 2024-2029
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,
        "XIX":19,"XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,
        "XXVII":27,"XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33}

_VOTE_TOKENS = ["WSTRZYMUJĘ SIĘ", "WSTRZYMUJE SIE", "NIE GŁOSOWAŁ", "NIE GŁOSOWALA",
                "NIEOBECNA", "NIEOBECNY", "PRZECIW", "ZA"]

_SES_TITLE_RE = re.compile(r'title="([IVXL]+)\s+sesja Rady Miejskiej w dniu[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*r?\.?"\s*\n?\s*href="(/(\d+))"', re.I)
_DOC_RE = re.compile(r'href="(\d+)/dokument/(\d+)"[^>]*>([^<]{0,120})')
_FILE_RE = re.compile(r'api/download/file\?id=(\d+)"[^>]*>(.*?)</a>', re.S)
_ROLL_RE = re.compile(
    r'(\d{1,2})\.\s+((?:[\wŁłŚśŻżĄąĘęŃńĆćÓó][\wŁłŚśŻżĄąĘęŃńĆćÓó\'-]*)(?:\s+[\wŁłŚśŻżĄąĘęŃńĆćÓó\'-]+)*)\s+'
    r'(WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIE|NIE GŁOSOWAŁ[AŁ]*|NIE GŁOSOWALA|NIEOBECNA|NIEOBECNY|PRZECIW|ZA)'
    r'(?=\s+\d{1,2}\.\s|\s*$)')

REQ_DELAY = 0.25
_LAST = 0.0
_sess = requests.Session()
_sess.headers["User-Agent"] = "Mozilla/5.0 (Radoskop)"


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir, binary=False):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + (".bin" if binary else ".dat"))
        if cf.is_file():
            data = cf.read_bytes()
            return data if binary else data.decode("utf-8", "ignore")
    _rate()
    r = _sess.get(url, timeout=60)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        (Path(cache_dir) / (key + (".bin" if binary else ".dat"))).write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def make_slug(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ---------------- discovery ----------------
def discover_sessions(cache):
    """/{sid} pages of IX-kadencja sessions from the Kadencja 2024-2029 category."""
    html = _get(BIP + KAD_CAT, cache)
    sessions = {}
    for m in _SES_TITLE_RE.finditer(html):
        roman, dd, mm, yyyy, href, sid = m.groups()
        date = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        if date < KAD_START:
            continue
        sessions[sid] = {"roman": roman.upper(), "date": date, "sid": sid}
    return sorted(sessions.values(), key=lambda s: s["date"])


def session_vote_pdfs(sid, cache):
    """Return list of (pdf_id, kind) vote printouts for a session page."""
    html = _get(f"{BIP}/{sid}", cache)
    docs = _DOC_RE.findall(html)
    out = []
    for _sid, did, dtxt in docs:
        flat = _nk(dtxt)
        if "pozostaleglosowania" in flat:
            gh = _get(f"{BIP}/{sid}/dokument/{did}", cache)
            for fid, ftxt in _FILE_RE.findall(gh):
                clean = re.sub(r"<[^>]+>", "", ftxt)
                if ".pdf" in clean.lower() and "glosowan" in _nk(clean):
                    out.append((fid, "other"))
        else:
            rh = _get(f"{BIP}/{sid}/dokument/{did}", cache)
            for fid, ftxt in _FILE_RE.findall(rh):
                clean = re.sub(r"<[^>]+>", "", ftxt)
                if _nk(clean).startswith("glosowanie") and ".pdf" in clean.lower():
                    out.append((fid, "resolution"))
    return out


# ---------------- PDF parsing ----------------
def parse_vote_pdf(pdf_bytes):
    """Parse a one-page per-vote printout. Returns dict or None."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as p:
        txt = "\n".join((pg.extract_text() or "") for pg in p.pages)
    if "Głosowanie" not in txt and "Glosowanie" not in txt:
        return None
    agg = {}
    for k, pat in (("za", r"Głosy za\s+(\d+)"),
                   ("przeciw", r"Głosy przeciw\s+(\d+)"),
                   ("wstrzymal_sie", r"Głosy wstrzymujące się\s+(\d+)")):
        m = re.search(pat, txt)
        if m:
            agg[k] = int(m.group(1))
    dm = re.search(r"Data głosowania:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", txt)
    vdate = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}" if dm else ""
    # topic: lines between "Głosowanie" header and "Typ głosowania"
    lines = txt.splitlines()
    topic_lines = []
    try:
        i0 = next(i for i, l in enumerate(lines) if l.strip() == "Głosowanie")
        i1 = next(i for i, l in enumerate(lines) if "Typ głosowania" in l)
        for l in lines[i0 + 1:i1]:
            s = l.strip()
            if not s or re.fullmatch(r"\d{1,3}", s):
                continue
            topic_lines.append(s)
    except StopIteration:
        pass
    topic = re.sub(r"\s+", " ", " ".join(topic_lines)).strip()
    named = defaultdict(list)
    roll_start = None
    for i, l in enumerate(lines):
        if "Uprawnieni do głosowania" in l:
            roll_start = i + 1
            break
    if roll_start is None:
        return None
    for l in lines[roll_start:]:
        if "Wydrukowano" in l:
            break
        for m in _ROLL_RE.finditer(l):
            name, vote = m.group(2).strip(), m.group(3)
            if vote == "ZA":
                cat = "za"
            elif vote == "PRZECIW":
                cat = "przeciw"
            elif vote.startswith("WSTRZYM"):
                cat = "wstrzymal_sie"
            elif vote.startswith("NIEOBECN"):
                cat = "nieobecni"
            else:
                cat = "nie_glosowal"
            named[cat].append(name)
    total_named = sum(len(v) for v in named.values())
    expected = sum(agg.values()) + len(named.get("nie_glosowal", [])) + len(named.get("nieobecni", []))
    ok = bool(agg) and total_named == expected
    return {"topic": topic, "vdate": vdate, "named": dict(named), "agg": agg,
            "named_total": total_named, "ok": ok}


# ---------------- output building (goleniow/lubin pattern) ----------------
def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    sessions_by_date = defaultdict(lambda: {"number": "", "vote_count": 0, "attendees": set()})
    all_votes = []
    vid = 0
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        sessions_by_date[d]["vote_count"] += 1
        if not sessions_by_date[d]["number"]:
            sessions_by_date[d]["number"] = rec.get("num", "")
        named = {k: v for k, v in rec["named"].items() if k in ("za", "przeciw", "wstrzymal_sie")}
        if rec["named"].get("nieobecni"):
            named["nieobecni"] = rec["named"]["nieobecni"]
        for cat in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal"):
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
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
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
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for v_id in common if vectors[a][v_id] == vectors[b][v_id])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
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
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"),
                             "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def canon_name(name, canon):
    """Normalize 'Nazwisko Imię'/'Imię Nazwisko' order to the canonical roster form.
    Handles glued names ('ŁawrynowiczZofia') by splitting case boundaries."""
    toks = name.split()
    if len(toks) == 1:
        # zrośnięte imię+nazwisko z PDF-u: split on lowercase->uppercase boundary
        parts = re.split(r'(?<=[a-złśżżąęńćó])(?=[A-ZŁŚŻĄĘŃĆÓ])', toks[0])
        if len(parts) >= 2:
            toks = parts
    if len(toks) < 2:
        return name
    key = tuple(sorted(_nk(t) for t in toks))
    return canon.get(key, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}
    canon = {tuple(sorted(_nk(t) for t in nm.split())): nm for nm in club_assign if len(nm.split()) >= 2}
    sessions = discover_sessions(cache)
    print(f"[stargard] {len(sessions)} sesji IX kad.")
    records = []
    bad = 0
    seen_pdf = set()
    for se in sessions:
        try:
            pdfs = session_vote_pdfs(se["sid"], cache)
            n_ok = 0
            for fid, kind in pdfs:
                if fid in seen_pdf:
                    continue
                seen_pdf.add(fid)
                pdf_bytes = _get(f"{BIP}/api/download/file?id={fid}", cache, binary=True)
                rec = parse_vote_pdf(pdf_bytes)
                if not rec:
                    continue
                if se["date"] < KAD_START:
                    continue
                # DWIE konwencje nazwisk w tym BIP: 'Imię Nazwisko' (uchwały) vs
                # 'Nazwisko Imię' (Pozostałe głosowania) — kanonizuj do formy rosteru.
                rec["named"] = {cat: [canon_name(nm, canon) for nm in names]
                                for cat, names in rec["named"].items()}
                rec["date"] = se["date"]
                rec["num"] = se["roman"]
                if not rec.get("ok"):
                    bad += 1
                records.append(rec)
                n_ok += 1
            print(f"  [{'ok' if n_ok else 'skip'}] {se['date']} {se['roman']:>4} votes={n_ok}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")
    if bad:
        print(f"[stargard] WARNING {bad} votes failed aggregate reconciliation")
    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[stargard] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
