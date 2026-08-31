#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""praszka main scraper — v5 regex parser (251/251 reconciled) + roster + save"""
import json, re, sys, unicodedata, argparse, urllib.request, ssl, hashlib
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
import fitz

CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"}
BASE = "https://bip.praszka.pl"
CAT = "6000"
KAD_START = "2024-05-07"
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"

BD_dir = None

NAME_RE = re.compile(r"^[A-ZŁŚŻŹÓ][\w\-_'’]+(?:\s+[A-ZŁŚŻŹÓ][\w\-_'’]+)+$")

_MON = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
        "lipca":7,"sierpnia":8,"września":9,"wrzesnia":9,"października":10,
        "pazdziernika":10,"listopada":11,"grudnia":12}


def extract_names(seg, max_n=None):
    names = []
    cur = ""
    for tok in re.split(r",", seg):
        tok = tok.strip()
        for piece in tok.split("\n"):
            piece = piece.strip().strip(".").strip()
            if not piece or re.match(r"^\d{1,4}$", piece):
                continue
            cur = (cur + " " + piece).strip() if cur else piece
            if NAME_RE.match(cur):
                if cur not in names:
                    names.append(cur)
                cur = ""
            elif re.match(r"^[a-ząćęłńóśźż]{1,3}\s", cur):
                cur = ""
    return names[:max_n] if max_n else names


def parse_votes(text):
    votes = []
    vote_pat = re.compile(
        r"([^\n]{5,250}?)\s*\(?(\d{1,2}:\d{2})\)?\s*\n+"
        r"Wyniki imienne\s*[:.]?\s*\n(.*?)"
        r"(?=\n[^\n]{5,250}?\s*\(?(\d{1,2}:\d{2})\)?\s*\n+Wyniki imienne|\Z)",
        re.S)
    for m in vote_pat.finditer(text):
        topic, hour, content = m.group(1).strip(), m.group(2), m.group(3)
        named = {}
        for label, key in [("ZA", "za"), ("PRZECIW", "przeciw"), ("PRZECEW", "przeciw"),
                           ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
                           ("NIE GŁOSOWALI/NIEOBECNI", "nieobecni"), ("NIE GŁOSOWALI", "nieobecni"),
                           ("NIEOBECNI", "nieobecni")]:
            pm = re.search(rf"{label}\s*\(?(\d+)\)?\s*[:.]?\s*\n(.*?)(?=\n(?:PRZECIW|PRZECEW|WSTRZYMUJĘ|NIE GŁOSOWALI|NIEOBECNI)|\Z)", content, re.S)
            if pm and key not in named:
                nn = int(pm.group(1))
                names = extract_names(pm.group(2), nn)
                named[key] = {"n": nn, "names": names}
        votes.append({"topic": f"{topic} ({hour})", "named": {k: v["names"] for k, v in named.items()},
                      "declared": {k: v["n"] for k, v in named.items()}})
    return votes


def slugify(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s.lower()) or "radny"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    def get(url, binary=False):
        key = hashlib.md5(url.encode()).hexdigest()
        if cache:
            ext = ".pdf" if binary else ".html"
            cf = cache / (key + ext)
            if cf.is_file():
                return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
            raw = r.read()
        if cache:
            ext = ".pdf" if binary else ".html"
            if binary:
                (cache / (key + ".pdf")).write_bytes(raw)
            else:
                try: text = raw.decode("utf-8")
                except Exception: text = raw.decode("cp1250", errors="replace")
                (cache / (key + ".html")).write_text(text, encoding="utf-8")
                return text
        if binary:
            return raw
        try: return raw.decode("utf-8")
        except Exception: return raw.decode("cp1250", errors="replace")

    # --- enumerate protocols
    html_cat = get(f"{BASE}/{CAT}/protokoly-z-posiedzen-rady-miejskiej-ix-kadencji-2024-2029.html")
    links = re.findall(r'href="(https://bip\.praszka\.pl/download/attachment/\d+/([^"?]+\.pdf)[^"]*)"', html_cat)
    seen = set(); pdfs = []
    for u, fn in links:
        if re.match(r"protokol-[\w-]+\.pdf$", fn) and u not in seen:
            seen.add(u); pdfs.append((u, fn))
    print(f"[praszka] protocol PDFs: {len(pdfs)}")

    ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
             "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
             "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
             "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,"XXXV":35}
    records = []
    sess_seen = set()
    for u, fn in pdfs:
        raw = get(u, binary=True)
        doc = fitz.open(stream=raw, filetype="pdf")
        text = "\n".join(pg.get_text("text") for pg in doc)
        head = text[:2500]
        dm = re.search(r"odbytej w dniu (\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+(\d{4})", head)
        if not dm:
            dm = re.search(r"kt[óo]ra odby[łl]a si[ęe] w dniu (\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+(\d{4})", head, re.I)
        if not dm:
            dm = re.search(r"dnia\s+(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+(\d{4})", head)
        iso = ""
        if dm:
            iso = f"{dm.group(3)}-{_MON[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
        fm = re.search(r"[Pp]rotok[óo][łl]\s*Nr\s*([IVXLCDM]+)/(\d{4})", head)
        snum = fm.group(1) if fm else ""
        if not iso or iso < KAD_START:
            continue
        print(f"  {snum}-quarter date={iso}")
        votes = parse_votes(text)
        n_ok = n_bad = 0
        for v in votes:
            d = v["declared"]; n = v["named"]
            okall = (d.get("za",0)==len(n.get("za",[])) and d.get("przeciw",0)==len(n.get("przeciw",[]))
                     and d.get("wstrzymal_sie",0)==len(n.get("wstrzymal_sie",[]))
                     and d.get("nieobecni",0)==len(n.get("nieobecni",[])))
            if okall: n_ok += 1
            else: n_bad += 1
        print(f"     votes={len(votes)} reconciled={n_ok} bad={n_bad}")
        key = (snum, iso)
        if key in sess_seen:
            continue
        sess_seen.add(key)
        for v in votes:
            records.append({"session_date": iso, "session_num": snum, "topic": v["topic"],
                            "named": {k: v["named"].get(k, []) for k in ("za","przeciw","wstrzymal_sie")}})
    print(f"[praszka] records: {len(records)}, sessions: {len(sess_seen)}")

    # build output (same builders as praszka v1)
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec["session_date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["session_num"], "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za","przeciw","wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec["session_num"],
                          "topic": rec["topic"], "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za","przeciw","wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {n: {"name": n, "club": "", "district": None,
                           "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0}
                       for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
                    if cat == "za": councilors_data[nm]["votes_za"] += 1
                    elif cat == "przeciw": councilors_data[nm]["votes_przeciw"] += 1
                    else: councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    csel = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                csel[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(csel.get(c["name"], set())) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": "", "district": None,
                                "frekwencja": round(frekwencja,1), "aktywnosc": round(aktywnosc,1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": 0,
                                "votes_nieobecny": 0, "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    ns = sorted(vectors.keys())
    for a, b in combinations(ns, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10: continue
        same = sum(1 for v2 in common if vectors[a][v2] == vectors[b][v2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same/len(common)*100,1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KAD, "label": KAD_LABEL, "clubs": {}, "sessions": sessions_data,
           "total_sessions": total_sessions, "total_votes": total_votes,
           "total_councilors": len(councilors_list), "councilors": councilors_list,
           "votes": all_votes, "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    output = {"generated": datetime.now().isoformat(), "default_kadencja": KAD, "kadencje": [kad]}

    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"votes":[]})
    for rec in records:
        d = rec["session_date"]
        for cat in ("za","przeciw","wstrzymal_sie"):
            for nm in rec["named"].get(cat, []):
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za","przeciw","wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        profiles.append({"name": nm, "slug": slugify(nm),
                         "kadencje": {KAD: {
                             "club": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / max(1,total_sessions) * 100, 1),
                             "aktywnosc": round((vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"]) / max(1, len(records)) * 100, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": 0, "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    profiles = {"profiles": profiles, "total": len(profiles)}

    out_path = city_dir / "docs" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad2 in output.get("kadencje", []):
        kid = kad2["id"]
        stubs.append({"id": kid, "label": kad2.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad2, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": output.get("generated",""), "default_kadencja": KAD,
                   "kadencje": stubs}, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[praszka] FINAL: votes={total_votes} sessions={total_sessions} councilors={len(councilors_list)}")


if __name__ == "__main__":
    main()
