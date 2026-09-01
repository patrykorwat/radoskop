#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Lubin — imienne głosowania Rady Miejskiej w Lubinie (IX kadencja).

Źródło: BIP bip.um.lubin.pl (custom CMS), kategoria "Kadencja 2024-2029"
(/artykuly/kadencja-2024-2029-2, paginacja ?page=N). Każdy artykuł
"Uchwały Rady Miejskiej podjęte na sesji {ROMAN} z {data}" zawiera załącznik
PDF "Imienny wykaz głosowań {ROMAN} sesji Rady Miejskiej w Lubinie" —
eksport platformy eSesja: na każdych 2 stronach jedno głosowanie
(strona 1: metryczka + "Łączne wyniki Za:/Przeciw:/Wstrzymał się:",
strona 2: tabela "Indywidualne wyniki uczestników": rola + imię nazwisko + głos + frekwencja).

Parser wierszy tekstowych: wiersz = "<Rola> Rady Miejskiej w Lubinie <imię nazwisko> <głos> <frekwencja>".
Głos: Za / Przeciw / Wstrzymał(a) się / Nie głosował(a). Frekwencja: Obecny / Nieobecny.
Walidacja: sumy imienne == agregaty "Łączne wyniki" per głosowanie.

Użycie:
    python scrape_lubin.py --city-dir <cities/lubin> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from html import unescape

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.um.lubin.pl"
KAD_LIST = "/artykuly/kadencja-2024-2029-2"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9, "pazdziernika": 10,
    "października": 10, "listopada": 11, "grudnia": 12,
}
_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,
        "XIX":19,"XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,
        "XXVII":27,"XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33}

_ROLE_RE = re.compile(
    r"^(Radna|Radny|Przewodniczący|Przewodnicząca|Wiceprzewodniczący|Wiceprzewodnicząca)"
    r"\s+Rady Miejskiej w Lubinie\s+")


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _parse_tail(toks):
    """Parse '<name...> <głos> [frekwencja]' tokens from the right.
    Returns (name_str, vote_cat, attendance) or (None, None, None).
    Głos: Za | Przeciw | Wstrzymał(a) się | Nie głosował(a).
    Frekwencja: Obecny/a | Nieobecny/a."""
    low = [_nk(t) for t in toks]
    cut = len(low)
    attendance = None
    if cut >= 1 and low[-1].startswith("nieobecn"):
        attendance, cut = "nieobecny", cut - 1
    elif cut >= 1 and low[-1].startswith("obecn"):
        attendance, cut = "obecny", cut - 1
    vote = None
    if cut >= 2 and low[cut - 2] == "nie" and low[cut - 1].startswith("glosowal"):
        vote, cut = "nie_glosowal", cut - 2
    elif cut >= 1 and low[cut - 1] == "za":
        vote, cut = "za", cut - 1
    elif cut >= 1 and low[cut - 1].startswith("przeciw"):
        vote, cut = "przeciw", cut - 1
    elif cut >= 2 and low[cut - 1] == "sie" and low[cut - 2].startswith("wstrzym"):
        vote, cut = "wstrzymal_sie", cut - 2
    elif cut >= 1 and low[cut - 1].startswith("wstrzym"):
        vote, cut = "wstrzymal_sie", cut - 1
    if not vote or cut < 1:
        return None, None, None
    return " ".join(toks[:cut]), vote, attendance


REQ_DELAY = 0.4
_LAST = 0.0


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
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        (Path(cache_dir) / (key + (".bin" if binary else ".dat"))).write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


# ---------------- discovery ----------------
def discover_sessions():
    """Return [{roman, date, article_url}] for IX kadencja session articles."""
    sessions = {}
    for page in range(1, 12):
        url = BIP + KAD_LIST + ("" if page == 1 else f"?page={page}")
        try:
            html = _get(url, None)
        except Exception:
            break
        found = 0
        for m in re.finditer(r'href="(/artykul/uchwaly-rady-miejskiej-podjete-na-sesji-([ivxl]+)(?:-nadzwyczajna)?-z-([0-9a-z-]+?)(?:r)?)["?]', html, re.I):
            href, roman_slug, dateslug = m.group(1), m.group(2).upper(), m.group(3)
            if href in sessions:
                continue
            roman = _ROM.get(roman_slug)
            dm = re.match(r"(\d{1,2})-([a-ząęółśżźćń]+)-(\d{4})", dateslug)
            if not roman or not dm:
                continue
            mon = _MONTHS.get(dm.group(2))
            if not mon:
                continue
            date = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
            if date < KAD_START:
                continue
            sessions[href] = {"roman": roman, "date": date, "article_url": BIP + href}
            found += 1
        if found == 0 and "Następna strona" not in html:
            break
    return sorted(sessions.values(), key=lambda s: s["date"])


def find_report_pdf(html, art_url):
    """Attachment URL of 'Imienny wykaz głosowań' inside an article page."""
    import html as _html_mod
    cands = []
    from urllib.parse import unquote
    for m in re.finditer(r'href="(/?[^"\s]*?/attachments/(\d+)/download[^"]*)"', html):
        href = _html_mod.unescape(m.group(1)).rstrip("&")
        decoded = unquote(unquote(href))
        flat = _nk(decoded)
        cands.append((href, ("imienny" in flat and "glosow" in flat) or ("wykaz" in flat and "glosow" in flat)))
    for href, is_report in cands:
        if is_report:
            return href if href.startswith("http") else BIP + href
    return None  # sesje I-XIV nie publikują imiennego wykazu — brak fallbacku na dowolny załącznik


# ---------------- PDF parsing ----------------
def parse_report(pdf_bytes):
    """Yield per-vote records from an eSesja-export PDF (2 pages per vote)."""
    votes = []
    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [(p.extract_text() or "") for p in pdf.pages]
    i = 0
    while i < len(texts):
        t = texts[i]
        if "Szczegóły głosowania" not in t:
            i += 1
            continue
        # header aggregates
        agg = {}
        for k, pat in (("za", r"^Za:\s*(\d+)"), ("przeciw", r"^Przeciw:\s*(\d+)"),
                       ("wstrzymal_sie", r"^Wstrzyma[łl]\s*(?:si[ęe]|się):\s*(\d+)")):
            m = re.search(pat, t, re.M)
            if m:
                agg[k] = int(m.group(1))
        topic = ""
        m = re.search(r"^Temat:\s*(.+)", t, re.M)
        if m:
            topic = m.group(1).strip()
        m2 = re.search(r"Opis:\s*(.+)", t)
        if m2 and len(m2.group(1).strip()) > len(topic):
            topic = m2.group(1).strip()
        # roll-call usually on next page
        named = defaultdict(list)
        j = i + 1
        while j < len(texts) and j <= i + 3:
            if "Indywidualne wyniki" in texts[j]:
                for line in texts[j].splitlines():
                    mm = _ROLE_RE.match(line.strip())
                    if not mm:
                        continue
                    rest = line.strip()[mm.end():]
                    toks = rest.split()
                    name, vt, att = _parse_tail(toks)
                    if not name:
                        continue
                    if att == "nieobecny":
                        vt_out = "nieobecni"
                    elif vt == "nie_glosowal":
                        vt_out = "nie_glosowal"
                    else:
                        vt_out = vt
                    named[vt_out].append(name)
                break
            j += 1
        total_named = sum(len(v) for v in named.values())
        expected = sum(agg.values()) + len(named.get("nie_glosowal", [])) + len(named.get("nieobecni", []))
        votes.append({"topic": topic, "named": dict(named), "agg": agg,
                      "named_total": total_named, "ok": total_named == expected or not agg})
        i = j + 1 if j > i else i + 1
    return votes


# ---------------- output building (goleniow pattern) ----------------
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
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        sessions_by_date[d]["attendees"].update(rec["named"].get("nie_glosowal", []))
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
    from itertools import combinations
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


def make_slug(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


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
    sessions = discover_sessions()
    print(f"[lubin] {len(sessions)} sesji IX kad.")
    records = []
    bad = 0
    for se in sessions:
        try:
            art = _get(se["article_url"], cache)
            pdf_url = find_report_pdf(art, se["article_url"])
            if not pdf_url:
                print(f"  [skip {se['date']} no imienny pdf]")
                continue
            pdf_bytes = _get(pdf_url, cache, binary=True)
            recs = parse_report(pdf_bytes)
            for r in recs:
                r["date"] = se["date"]
                r["num"] = se["roman"]
                if not r.get("ok"):
                    bad += 1
            records += recs
            print(f"  [ok] {se['date']} {se['roman']:>3} votes={len(recs)}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")
    if bad:
        print(f"[lubin] WARNING {bad} votes failed aggregate reconciliation")
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
    print(f"[lubin] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
