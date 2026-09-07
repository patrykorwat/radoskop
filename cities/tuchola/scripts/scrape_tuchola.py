#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Tuchola — scraper głosowań imiennych (Madkom BIP SPA API + DSSS Vote PDF-y).

Źródło: https://bip.tuchola.pl — Madkom "Nowoczesny BIP" React SPA; JSON API bez klucza:
  /api/menu/1128/articles?limit=N&offset=M   -> lista artykułów "Imienne wykazy głosowań radnych"
  /api/articles/{id}                          -> artykuł z attachments[{id,...}]
  /api/files/{fid}                            -> PDF (DSSS Vote wydruk, tekstowy)

PDF (1 strona/głosowanie): nagłówek 'na <ROMAN> sesji w dniu <date>',
'Uchwała została podjęta następującą proporcją głosów: jestem za N, jestem przeciw N, wstrzymało się N.'
Dwukolumnowy układ słów: lewa kolumna (x<340): 'Jestem za' lista, niżej 'Wstrzymuję się' lista;
prawa kolumna (x>=340): 'Jestem przeciw' lista, niżej 'Obecni radni, którzy nie wzięli udziału'.
Nazwy numerowane 'N. Imię Nazwisko'. Walidacja per głos: sumy == agregaty.
"""
import argparse
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pymupdf

API = "https://bip.tuchola.pl/api"
MENU_ID = 1128  # Imienne wykazy głosowań radnych > Sesje Rady Miejskiej > RADA MIEJSKA
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
COL_SPLIT_X = 340.0
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
          "lipca":7,"sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12}


def _get(url, cache=None, binary=False, tries=3):
    key = None
    if cache is not None:
        import hashlib
        key = cache / (hashlib.md5(url.encode()).hexdigest() + (".pdf" if binary else ".json"))
        if key.is_file():
            return key.read_bytes() if binary else key.read_text(encoding="utf-8")
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                data = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (a + 1))
    else:
        raise last
    if key is not None:
        cache.mkdir(parents=True, exist_ok=True)
        key.write_bytes(data)
    return data if binary else data.decode("utf-8", "replace")


def roman_to_int(s):
    vals = {"I":1,"V":5,"X":10,"L":50,"C":100}
    tot = prev = 0
    for ch in reversed(s):
        v = vals[ch]
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


def discover_articles(max_pages=12):
    """Paginate the votes menu (sorted by publishDate desc). publishDate is NOT the
    session date (older sessions can be backfilled later), so we cannot stop early
    on a date cutoff — walk a bounded number of pages instead."""
    arts = []
    offset = 0
    limit = 100
    for _page in range(max_pages):
        d = json.loads(_get(f"{API}/menu/{MENU_ID}/articles?limit={limit}&offset={offset}"))
        lst = d.get("articles", d.get("list", []))
        if not lst:
            break
        for it in lst:
            pub = ""
            for f in it.get("columnFields", []):
                if f.get("fieldId") == 26:
                    pub = f.get("value", "")
            title = ""
            for f in it.get("aliasFields", []):
                if f.get("alias") == "title":
                    title = f.get("value", "")
            arts.append({"id": it["id"], "title": title, "pub": pub})
        offset += limit
        time.sleep(0.25)
    return arts


BOUND_X = 300.0  # column split for per-name zone assignment (left lists <300, right lists >=300)


def _parse_vote_page(words):
    """Parse one page's words [(x0,y0,x1,y1,word)] into a validated vote dict or None."""
    full = " ".join(w[4] for w in sorted(words, key=lambda w: (round(w[1]), w[0])))
    full = re.sub(r"\s+", " ", full)
    m = re.search(r"na ([IVXLCDM]+) sesji w dniu (\d{1,2}) ([\wżś]+) (\d{4}) r\.?", full)
    if not m:
        return None
    roman, day, mon, year = m.group(1), int(m.group(2)), m.group(3).lower(), int(m.group(4))
    if mon not in MONTHS:
        return None
    date = f"{year:04d}-{MONTHS[mon]:02d}-{day:02d}"
    agg_m = re.search(r"jestem za\s*(\d+),\s*jestem przeciw\s*(\d+),\s*wstrzymało się\s*(\d+)", full)
    if not agg_m:
        return None
    agg = {"za": int(agg_m.group(1)), "przeciw": int(agg_m.group(2)), "wstrzym": int(agg_m.group(3))}
    tm = re.search(r"głosowania:?\s*(\d{2})\.(\d{2})\.(\d{4})\s*(\d{2}:\d{2}:\d{2})", full)
    vote_time = f"{tm.group(3)}-{tm.group(2)}-{tm.group(1)} {tm.group(4)}" if tm else date
    topic_m = re.search(r"Uchwała numer\s+(\S+)\s+(.*?)(?:Uchwała została|została podjęta następującą)", full)
    topic = ((topic_m.group(1) + " " + topic_m.group(2)) if topic_m else "").strip()
    # --- zone headers from word positions ---
    # za header = last row containing 'za' (left) and 'przeciw' (right); pages can carry
    # the PREVIOUS vote's footer spillover at the top (Wstrzymuję/Obecni/BRAK artifacts),
    # so wstrzym/obecni headers count only when they sit BELOW the za header.
    band_za = defaultdict(lambda: [False, False])
    for x0, y0, _x1, _y1, w in words:
        b = round(y0 / 5)
        if w == "za" and x0 < BOUND_X:
            band_za[b][0] = True
        if w == "przeciw" and x0 >= BOUND_X:
            band_za[b][1] = True
    za_bands = [b for b, (l, r) in band_za.items() if l and r]
    if not za_bands:
        return None
    za_hdr_y = max(za_bands) * 5
    _c = [y0 for x0, y0, _1, _2, w in words if w == "Wstrzymuję" and y0 > za_hdr_y]
    wstrz_hdr_y = min(_c) if _c else float("inf")
    _o = [y0 for x0, y0, _1, _2, w in words if w == "Obecni" and x0 >= BOUND_X and y0 > za_hdr_y]
    obecni_hdr_y = min(_o) if _o else float("inf")
    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "obecni_niegl": []}
    rows = defaultdict(list)
    for w in words:
        rows[round(w[1] / 5)].append(w)
    lp_re = re.compile(r"^\d+\.$")
    skip_words = {"BRAK", "Operator", "systemu:", "Wygenerowano", "radni,", "udziału",
                  "w", "wzięli", "głosowaniu", "Lepak.", "App.", "DSSS", "Vote",
                  "za", "przeciw", "Jestem", "została", "podjęta", "w", "trybie",
                  "jawnym,", "zwykłą", "bezwzględną", "większością", "ustawowego",
                  "składu", "rady", "głosów", "głosów.", "Data", "i", "godzina:",
                  "i", "godzina", "głosowania:"}
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda w: w[0])
        if not any(lp_re.match(w[4]) for w in ws):
            continue
        # split this row's words at each Lp token: group = Lp..next Lp
        idxs = [i for i, w in enumerate(ws) if lp_re.match(w[4])]
        for k, i in enumerate(idxs):
            grp = ws[i + 1: idxs[k + 1] if k + 1 < len(idxs) else len(ws)]
            lp_x = ws[i][0]
            side = "left" if lp_x < BOUND_X else "right"
            names = [w[4] for w in grp
                     if w[4] not in skip_words and not w[4].isdigit()
                     and w[4].lower() != "brak"]
            if not names:
                continue
            nm = re.sub(r"\s+", " ", " ".join(names)).strip()
            # guard: names shouldn't contain header junk
            if any(c in nm for c in "0123456789"):
                continue
            ytop = grp[0][1] if grp else ws[i][1]
            if side == "left":
                zone = "za" if ytop < wstrz_hdr_y else "wstrzymal_sie"
            else:
                zone = "przeciw" if ytop < obecni_hdr_y else "obecni_niegl"
            named[zone].append(nm)
    counter = {"za": len(named["za"]), "przeciw": len(named["przeciw"]), "wstrzym": len(named["wstrzymal_sie"])}
    if any(counter[k] != agg[k] for k in counter):
        return {"_fail": (counter, agg), "date": date, "topic": topic, "time": vote_time}
    return {"date": date, "time": vote_time, "roman": roman_to_int(roman), "topic": topic, "named": named}


def parse_pdf(data):
    """Each page = one vote (DSSS spooler can emit >1 page/article; dedupe downstream by time)."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    out = []
    for p in doc:
        words = [(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
        if not words:
            continue  # scanned page -> skip
        r = _parse_vote_page(words)
        if r is not None:
            out.append(r)
    if not out:
        return None
    return out


def _name_key(nm):
    import unicodedata as _u
    s = _u.normalize("NFKD", nm.lower().replace("ł", "l"))
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not _u.combining(c)))


def normalize_roster(records):
    """Merge spelling variants of the same councillor (source typos like
    Drewczyńska/Drewczyńśka) — canonical = most frequent spelling per surname-key."""
    freq = {}
    for r in records:
        for cat, names in r["named"].items():
            for nm in names:
                freq[nm] = freq.get(nm, 0) + 1
    canon = {}
    for nm in sorted(freq, key=lambda n: -freq[n]):
        k = _name_key(nm.split()[-1]) + "|" + _name_key(nm.split()[0])
        canon.setdefault(k, nm)
    remap = {}
    for nm in freq:
        k = _name_key(nm.split()[-1]) + "|" + _name_key(nm.split()[0])
        if canon[k] != nm:
            remap[nm] = canon[k]
    if remap:
        print(f"[tuchola] roster typo merge: {remap}")
        for r in records:
            for cat in r["named"]:
                r["named"][cat] = [remap.get(n, n) for n in r["named"][cat]]
                # dedupe within a category (same person listed under both spellings)
                seen = set()
                r["named"][cat] = [n for n in r["named"][cat] if not (n in seen or seen.add(n))]
    return records


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": str(rec.get("roman", "")),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        s = sessions_by_date[d]
        s["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items() if k != "obecni_niegl"}
        for cat in ("za", "przeciw", "wstrzymal_sie", "obecni_niegl"):
            s["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": str(rec.get("roman", "")),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
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
    for s in sessions_data:
        all_names.update(s["attendees"])
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            key = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal"}[cat]
            for nm in names:
                if nm in councilors_data:
                    councilors_data[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for s in sessions_data:
        for nm in s["attendees"]:
            councillor_sess[nm].add(s["date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": 0,
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
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
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
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    sess_set = set()
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        sess_set.add(d)
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in rec["named"].get(cat, []):
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
        for nm in rec["named"].get("obecni_niegl", []):
            cv.setdefault(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    n_sessions = len(sess_set) or 1
    profiles = []
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / total * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-articles", type=int, default=0)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    arts = discover_articles()
    if args.max_articles:
        arts = arts[: args.max_articles]
    print(f"[tuchola] candidate articles (pub>=cutoff window): {len(arts)}")

    records = []
    fails = 0
    skipped_old = 0
    for a in arts:
        # a vote from a session >= KAD_START must have been published >= KAD_START
        # (publish-in-the-past is impossible), so pre-cutoff publications are safely skipped
        if (a.get("pub") or "")[:10] < KAD_START:
            skipped_old += 1
            continue
        try:
            art = json.loads(_get(f"{API}/articles/{a['id']}", cache))
            atts = art.get("attachments") or []
            if not atts:
                continue
            fid = atts[0]["id"]
            pdf = _get(f"{API}/files/{fid}", cache, binary=True)
            rs = parse_pdf(pdf)
            if not rs:
                fails += 1
                continue
            kept_any = False
            for r in rs:
                if "_fail" in r:
                    fails += 1
                    print(f"  [FAIL-validate {a['id']}] {r['_fail']}")
                    continue
                if r["date"] < KAD_START:
                    skipped_old += 1
                    continue
                # prefer cleaner topic from article title
                t = a.get("title") or ""
                t = re.sub(r"^Imienny wykaz głosowań radnych\s*-\s*", "", t, flags=re.I)
                if t:
                    r["topic"] = t
                records.append(r)
                kept_any = True
        except Exception as e:  # noqa: BLE001
            print(f"  [ERR {a['id']}] {type(e).__name__}: {e}")
        time.sleep(0.15)
    print(f"[tuchola] parsed votes={len(records)} fails={fails} pre-IX skipped={skipped_old}")
    # dedupe by exact voting timestamp (spooler can re-emit the same vote in several articles)
    seen_t = set()
    uniq = []
    for r in sorted(records, key=lambda r: r.get("time", r["date"])):
        k = r.get("time") or (r["date"], r["topic"])
        if k in seen_t:
            continue
        seen_t.add(k)
        uniq.append(r)
    if len(uniq) != len(records):
        print(f"[tuchola] deduped by vote timestamp: {len(records)} -> {len(uniq)}")
    records = sorted(uniq, key=lambda r: r["date"])
    records = normalize_roster(records)
    output, tv, ts = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tuchola] DONE votes={tv} sessions={ts} councilors={profiles['total']}")


if __name__ == "__main__":
    main()
