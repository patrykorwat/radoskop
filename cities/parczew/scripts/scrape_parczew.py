#!/usr/bin/env python3
"""Radoskop Parczew — custom scraper (Wrota Lubelszczyzny BIP + text vote PDFs).

Backend: https://umparczew.bip.lubelskie.pl (Rada Miejska w Parczewie, IX kadencja).
Parczew publishes per-session "Wykaz z głosowań na sesji NN" PDFs under the
"wykaz głosowań na sesjach" category (id=309). The category listing is served
via a DataTables AJAX endpoint (?id=309&action=list-ajax) returning JSON, and
each document detail (?id=309&action=details&document_id=<id>) links a
text-layer PDF (upload/pliki/*.pdf). Each PDF contains full per-councilor
imienne votes:

  <N>. Głosowanie w sprawie {topic} - czas głosowania: {...}, wyniki:
       ZA: {n}, PRZECIW: {m}, WSTRZYMUJĘ SIĘ: {k}, BRAK GŁOSU: {x}, NIEOBECNI: {y}
  Wyniki imienne: {Name1} (ZA), {Name2} (PRZECIW), ..., {NameN} (NIEOBECNI)

Text-based (no OCR). 28 session reports for IX kadencja (2024-05-07 .. 2026-08-07).

Output: docs/kadencja-2024-2029.json + docs/data.json + docs/profiles.json
(format identical to blonie/police/morag).
Dodane automatycznie w ramach ekspansji cities 2026-08-28.
"""

import argparse, json, re, sys
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import pdfplumber

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BIP = "https://umparczew.bip.lubelskie.pl"
CAT = "309"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
HDRS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
           "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
           "pazdziernika": 10, "października": 10, "listopada": 11,
           "grudnia": 12}
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
          "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
          "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19,
          "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25,
          "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30,
          "XXXI": 31, "XXXII": 32, "XXXIII": 33, "XXXIV": 34, "XXXV": 35,
          "XXXVI": 36, "XXXVII": 37, "XXXVIII": 38, "XXXIX": 39, "XL": 40}

_VOTEKEY = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
            "WSTRZYMUJE SIĘ": "wstrzymal_sie", "BRAK GŁOSU": "brak",
            "NIEOBECNI": "nieobecni"}


def _get(url, cache_dir=None, timeout=40):
    if cache_dir:
        import hashlib
        h = hashlib.md5(url.encode()).hexdigest()[:16]
        p = Path(cache_dir) / (h + ".bin")
        if p.is_file():
            return p.read_bytes()
    r = requests.get(url, headers=HDRS, timeout=timeout, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        p = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest()[:16] + ".bin")
        p.write_bytes(data)
    return data


def discover_docs(cache_dir=None, first=None, last=None):
    """Pobiera liste dokumentow 'Wykaz z glosowan' z kategorii 309 (AJAX JSON)."""
    url = f"{BIP}/index.php?id={CAT}&action=list-ajax"
    raw = _get(url, cache_dir)
    d = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for a in d.get("aaData", []):
        dt = a.get("data_utworzenia", "")
        if dt < KAD_START:
            continue
        out.append({"id": a["id_dokumentu"], "date": dt, "title": a.get("tresc", ""),
                    "znak": a.get("znak", "")})
    out.sort(key=lambda x: (x["date"], x["id"]))
    if first is not None:
        out = out[first:last]
    return out


def doc_pdf_url(doc_id, cache_dir=None):
    """Zwraca URL PDF (upload/pliki/*.pdf) dla dokumentu (lub None)."""
    url = f"{BIP}/index.php?id={CAT}&action=details&document_id={doc_id}"
    html = _get(url, cache_dir).decode("utf-8", "replace")
    m = re.search(r'href="([^"]*upload/pliki/[^"]*\.pdf)"', html)
    if m:
        u = m.group(1)
        if u.startswith("/"):
            return BIP + u
        return u
    return None


def parse_pdf_imienne(pdf_bytes):
    """Parsuje PDF raportu -> (session_meta, list votes) | (None, [])."""
    try:
        with pdfplumber.open(io_bytes(pdf_bytes)) as pdf:
            pages = [pg.extract_text() or "" for pg in pdf.pages]
    except Exception:
        return None, []
    full = "\n".join(pages)
    # session header
    roman = "|".join(sorted(_ROMAN.keys(), key=len, reverse=True))
    m = re.search(rf'\b({roman})\s+Sesja\s+w\s+dniu\s+(\d{{1,2}})\s+([a-ząćęłńóśźż]+)\s+(\d{{4}})', full)
    sess_date = None; sess_num = None
    if m:
        mm = _MONTHS.get(m.group(3).lower())
        if mm:
            sess_date = f"{m.group(4)}-{mm:02d}-{int(m.group(2)):02d}"
        sess_num = _ROMAN.get(m.group(1))
    blocks = re.split(r'\n\s*(\d+)\.\s*G[łl]osowanie\s+w\s+spraw[iely]+', full)
    votes = []
    i = 1
    while i < len(blocks):
        num = blocks[i]; body = blocks[i + 1]
        mc = re.search(r'wyniki:\s*ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJ[ĘE]\s*SI[ĘE]:\s*(\d+)', body)
        mt = re.search(r'(.*?)\s*-\s*czas\s+g[łl]osowania', body, re.S)
        topic = (mt.group(1).strip()[:200] if mt else "")
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": [], "nieobecni": []}
        if "Wyniki imienne:" in body:
            seg = body.split("Wyniki imienne:", 1)[1]
            # Nazwiska łamią się na granicach linii (np. 'Marek\nChwalczuk (ZA)');
            # scal newlines do spacji, by regex łapał pełne 'Imię Nazwisko'.
            seg = re.sub(r"\s*\n\s*", " ", seg)
            for mm2 in re.finditer(r'([A-ZĄĆĘŁŃÓŚŹŻ][^(),]{1,50}?)\s*\((\w+(?:\s+\w+)?)\)', seg):
                vk = _VOTEKEY.get(mm2.group(2))
                if vk:
                    named[vk].append(mm2.group(1).strip())
        counts = (int(mc.group(1)), int(mc.group(2)), int(mc.group(3))) if mc else (None, None, None)
        votes.append({"num": num, "topic": topic, "counts": counts, "named": named})
        i += 2
    return {"date": sess_date, "num": sess_num}, votes


def io_bytes(b):
    import io
    return io.BytesIO(b)


def validate_vote(v):
    """Sprawdza czy liczba nazwisk w kategoriach == agregat. Zwraca (ok, msg)."""
    za, prz, wst = v["counts"]
    nza = len(v["named"]["za"]); nprz = len(v["named"]["przeciw"])
    nwst = len(v["named"]["wstrzymal_sie"])
    if za is None:
        return False, "no aggregate"
    if nza != za or nprz != prz or nwst != wst:
        return False, f"agg=({za},{prz},{wst}) parsed=({nza},{nprz},{nwst})"
    return True, "ok"


# ---- output (reuses the blonie/police/morag pattern) ----

def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E',
            'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
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
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in
                                     ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"],
                              "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"),
            "district": None, "votes_za": 0, "votes_przeciw": 0,
            "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0,
            "rebellions": []}
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
    total_votes = len(all_votes); total_sessions = len(sessions_data)
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
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
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
                      "score": round(same / len(common) * 100, 1),
                      "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "brak": 0, "nieobecni": 0, "votes": []})
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
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"),
                "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else (Path(args.work_dir) if args.work_dir else city_dir / "work")
    cache.mkdir(parents=True, exist_ok=True)

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = dict(cfg.get("club_assignments", {}) or {})

    docs = discover_docs(cache)
    print(f"[parczew] {len(docs)} raportow IX kad. (2024-2029)")
    records = []
    pdf_dir = cache / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        pdf_url = doc_pdf_url(doc["id"], cache)
        if not pdf_url:
            print(f"  [NO-PDF {doc['date']}] {doc['title']}")
            continue
        data = _get(pdf_url, cache)
        sess, votes = parse_pdf_imienne(data)
        if not votes:
            print(f"  [NO-IMIENNE {doc['date']}] {doc['title']}")
            continue
        sdate = sess.get("date") or doc["date"]
        snum = sess.get("num")
        tmp = []
        for v in votes:
            ok, msg = validate_vote(v)
            if ok:
                v["date"] = sdate; v["num"] = snum
                tmp.append(v)
            else:
                print(f"    [VAL-FAIL {sdate}] {msg}")
        records += tmp
        print(f"  [ok] {sdate} nr{snum} votes={len(tmp)}")
    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs_dir = city_dir / "docs"; docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs_dir / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[parczew] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
