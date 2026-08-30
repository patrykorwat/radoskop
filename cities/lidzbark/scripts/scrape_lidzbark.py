#!/usr/bin/env python3
"""Radoskop Lidzbark — msesja scraper (Portal Informacyjny Rady Miejskiej w Lidzbarku).

Źródło: https://lidzbark.msesja.pl/ — platforma msesja.pl ("Portal Informacyjny
Rady Miejskiej", serwerowo renderowana, nie wymaga JS). Publikuje:

  /wyniki-glosowan?page=N   — lista głosowań imiennych (50/page, page=7 najstarsza:
                               II sesja 2024-05-15; page=1 najnowsza: 2026-07-22)
  /wyniki-glosowan/{id}     — szczegóły głosowania: 3 bloki votingUsers (ZA/PRZECIW/
                               WSTRZYMAŁO SIĘ) + sekcja "Nieobecni (n)" + link PDF
  /sesje                    — 29 sesji (II..XXX, IX kadencja 2024-2029) z datami + miejscem
  /sklad-rady               — 15 radnych IX kadencji (profil links)
  /protokoly                — protokoły PDF

PUŁAPKA (zweryfikowana na 4 głosowaniach): platforma wstawia NIEOBECNYCH radnych
do bloku "WSTRZYMAŁO SIĘ" (badge abstain), a aggregate "barItem abstain" liczy
tylko faktycznych wstrzymujących się. Rozwiązanie: true_wstrzymal = blok_abstain
− nieobecni (reconcile 100%: vid 2929: 8−1=7✓, 2952: 5−5=0✓, 2722: 6−3=3✓,
2849: 8−3=5✓). Radni obecni ale z brakiem głosu (jeśli wystąpią po odjęciu)
→ kategoria "nie_glosowal" (brak osobnego bloku w msesja).

Output: docs/kadencja-2024-2029.json + docs/data.json + docs/profiles.json
(format = olesno/parczew/blonie/police: named_votes {za,przeciw,wstrzymal_sie,
nie_glosowal,nieobecny,brak} — listy nazwisk; counts za/przeciw/wstrzymal_sie).
"""
import argparse
import json
import re
import time
import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://lidzbark.msesja.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
HDRS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
          "pazdziernika": 10, "listopada": 11, "grudnia": 12}

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
          "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20, "xxi": 21,
          "xxii": 22, "xxiii": 23, "xxiv": 24, "xxv": 25, "xxvi": 26, "xxvii": 27,
          "xxviii": 28, "xxix": 29, "xxx": 30, "xxxi": 31, "xxxii": 32, "xxxiii": 33,
          "xxxiv": 34, "xxxv": 35, "xxxvi": 36, "xxxvii": 37, "xxxviii": 38,
          "xxxix": 39, "xl": 40}


def _get(url, cache_dir=None, ttl=86400.0):
    """GET with file cache (md5 key, TTL) — polite on the civic portal."""
    if cache_dir:
        h = hashlib.md5(url.encode()).hexdigest()[:16]
        p = Path(cache_dir) / (h + ".html")
        if p.is_file() and (time.time() - p.stat().st_mtime) < ttl:
            return p.read_text(encoding="utf-8", errors="replace")
    r = requests.get(url, headers=HDRS, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    text = r.text
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        p = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest()[:16] + ".html")
        p.write_text(text, encoding="utf-8")
        time.sleep(0.4)
    return text


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def discover_vote_ids(cache_dir=None):
    """All vote ids from /wyniki-glosowan (paginated, newest first)."""
    ids = []
    page = 1
    while True:
        url = f"{BASE}/wyniki-glosowan" if page == 1 else f"{BASE}/wyniki-glosowan?page={page}"
        t = _get(url, cache_dir)
        page_ids = sorted(int(i) for i in re.findall(r'/wyniki-glosowan/(\d{3,5})', t))
        new = [i for i in page_ids if i not in ids]
        if not new:
            break
        ids.extend(new)
        page += 1
        if page > 12:
            break
    return sorted(set(ids))


def list_rows_meta(cache_dir=None):
    """(id -> {date, time, session_label, topic, агрегat counts}) from list pages."""
    meta = {}
    for page in range(1, 13):
        url = f"{BASE}/wyniki-glosowan" if page == 1 else f"{BASE}/wyniki-glosowan?page={page}"
        t = _get(url, cache_dir)
        rows = re.findall(r'<tr>\s*<td>(\d{4}-\d{2}-\d{2}), (\d{2}:\d{2})</td>\s*<td>(.*?)</tr>', t, re.S)
        if not rows:
            break
        new = 0
        for d, tm, body in rows:
            mid = re.search(r'/wyniki-glosowan/(\d+)', body)
            if not mid or mid.group(1) in meta:
                continue
            new += 1
            vid = mid.group(1)
            lab = re.search(r'class="small">(.*?)</span>', body, re.S)
            label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab.group(1))).strip() if lab else ""
            rom = re.search(r'([ivxlcdm]+)\s+sesj', label.lower())
            sid = _ROMAN.get(rom.group(1)) if rom else None
            tit = re.search(r'<b>(.*?)</b>', body, re.S)
            topic = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tit.group(1))).strip() if tit else ""
            bars = re.findall(r'barItem (\w+)">.*?strVal"><i class="fa fa-user"></i> (\d+)</div>', body, re.S)
            agg = {k: int(v) for k, v in bars}
            meta[vid] = {"date": d, "time": tm, "session_label": label,
                         "session_num": sid, "topic": topic,
                         "agg": {"za": agg.get("infavour", 0), "przeciw": agg.get("against", 0),
                                 "wstrzymal": agg.get("abstain", 0)},
                         "status": "ZATWIERDZONO" if "ZATWIERDZONO" in body else
                                   ("ODRZUCONO" if "ODRZUCONO" in body else "")}
        if new == 0:
            break
    return meta


def parse_vote_detail(vid, cache_dir=None):
    """Per-councilor votes from /wyniki-glosowan/{id} detail page.

    Layout (verified on 6 votes incl. all edge cases):
      * main zone (before <hr>): three votingUsers columns — ZA / PRZECIW /
        WSTRZYMAŁO SIĘ, each col = badge + barItem aggregate + userItem names.
        barItem counts = the TRUE aggregate; users listed ONLY if they
        actually cast that vote (list-aggregate reconcile 12/12 after
        hr-split fix).
      * post-<hr> zone: <h5> sections with declared (n):
          - "Nie zagłosowali" — present but didn't vote (incl. formalny wniosek)
          - "Nieobecni" — absent
      * "GŁOSOWANIE ANULOWANE" banner possible (e.g. 2878) — flag, data as shown.
    """
    t = _get(f"{BASE}/wyniki-glosowan/{vid}", cache_dir)
    i = t.find('class="votingShow"')
    if i == -1:
        return None
    chunk = t[i:]
    anulowane = "GŁOSOWANIE ANULOWANE" in chunk
    hrpos = chunk.find("<hr>")
    main, post = (chunk, "") if hrpos == -1 else (chunk[:hrpos], chunk[hrpos:])
    blocks = {}
    marks = [m.start() for m in re.finditer(r'<div class="votingUsers">', main)]
    marks.append(len(main))
    for n in range(len(marks) - 1):
        seg = main[marks[n]:marks[n + 1]]
        b = re.search(r'badge badge-([a-z]+)">', seg)
        users = re.findall(r'userItemName"><a href="[^"]+" title="([^"]+)">', seg)
        if b:
            kind = {"infavour": "za", "against": "przeciw", "abstain": "wstrzymal"}.get(b.group(1))
            if kind:
                blocks[kind] = users
    sections = {}
    hmatches = list(re.finditer(r'<h5>\s*([^<]{3,40}?)\s*\((\d+)\)</h5>', post))
    if hmatches:
        for n, hm in enumerate(hmatches):
            hdr = hm.group(1)
            start = hm.start()
            end = hmatches[n + 1].start() if n + 1 < len(hmatches) else len(post)
            names = re.findall(r'userItemName"><a href="[^"]+" title="([^"]+)">', post[start:end])
            sections[hdr] = names
    return {"blocks": blocks, "nie_zaglosowali": sections.get("Nie zagłosowali", []),
            "nieobecni": sections.get("Nieobecni", []), "anulowane": anulowane, "html": t}


def parse_sessions(cache_dir=None):
    """Sessions from /sesje: (roman, date, time, place)."""
    t = _get(f"{BASE}/sesje", cache_dir)
    out = {}
    rows = re.findall(r'<td>(\d{4}-\d{2}-\d{2})</td>\s*<td>.*?(\d{2}:\d{2}).*?/sesja/(\d+),porzadek-obrad,([a-z0-9_]+)', t, re.S)
    for d, tm, _sid, slug in rows:
        rm = re.match(r'([ivxlcdm]+)_sesja', slug)
        if not rm:
            continue
        rom = _ROMAN.get(rm.group(1))
        label = re.sub(r"_", " ", slug)
        if rom and d not in out:
            out[d] = {"date": d, "time": tm, "roman": rom,
                      "label": f"{rm.group(1).upper()} sesja Rady Miejskiej"}
    return out


def parse_roster(cache_dir=None):
    """Roster from /sklad-rady: dict name -> {role, profile_url, photo}."""
    t = _get(f"{BASE}/sklad-rady", cache_dir)
    out = {}
    soup = _soup(t)
    for box in soup.select(".userItemBox"):
        a = box.select_one(".userItemBoxName a")
        if not a:
            continue
        name = a.get("title") or a.get_text(strip=True)
        ttl = box.select_one(".userItemBoxTitle a")
        role = ttl.get_text(strip=True) if ttl else "Radny"
        img = box.select_one("img")
        out[name] = {"role": role,
                     "profile_url": a.get("href", ""),
                     "photo_url": img.get("src", "") if img else ""}
    return out


def build_roster_from_votes(votes_records):
    """Union of all names seen in vote records (covers mid-term turnover)."""
    names = set()
    for rec in votes_records:
        for k, lst in rec["named"].items():
            names.update(lst)
    for rec in votes_records:
        names.update(rec["nieobecni"])
    return names


def reconcile(rec):
    """Build named dict from hr-split blocks + post-hr sections (no pitfall left)."""
    blocks = dict(rec["blocks"])
    named = {
        "za": list(blocks.get("za", [])),
        "przeciw": list(blocks.get("przeciw", [])),
        "wstrzymal_sie": list(blocks.get("wstrzymal", [])),
        "nie_glosowal": list(rec.get("nie_zaglosowali", [])),
        "nieobecny": list(rec.get("nieobecni", [])),
        "brak": [],
    }
    return named


def validate(rec, named):
    agg = rec["agg"]
    for key in ("za", "przeciw", "wstrzymal_sie"):
        n = len(named.get(key, []))
        c = agg.get({"za": "za", "przeciw": "przeciw", "wstrzymal_sie": "wstrzymal"}[key], 0)
        if n != c:
            return False, f"{rec['vid']} {rec['date']}: {key} agg={c} parsed={n}"
    return True, "ok"


def make_slug(name):
    repl = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s",
            "ź": "z", "ż": "z", "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N",
            "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z"}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def build_output(records, club_assign, roles_map, sessions_info):
    all_votes = []
    vid_n = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = rec["named"]
        for cat in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal", "brak"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        vid_n += 1
        all_votes.append({
            "id": str(vid_n), "session_date": d, "session_number": rec.get("session_num", ""),
            "topic": rec.get("topic", ""), "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            "source_url": f"{BASE}/wyniki-glosowan/{rec['vid']}",
        })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        si = sessions_info.get(d, {})
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    for rec in records:
        if rec["date"] and rec["date"] >= KAD_START:
            all_names.update(rec.get("nieobecni", []))
    # also include roster names (never voted?) as councilors
    councilors_data = {}
    for name in sorted(all_names):
        club = club_assign.get(name, "NZ")
        role = roles_map.get(name, "")
        councilors_data[name] = {"name": name, "club": club, "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": [],
                                 "role": role}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm2 in names:
                if nm2 not in councilors_data:
                    continue
                if cat == "nieobecny":
                    councilors_data[nm2]["votes_nieobecny"] += 1
                elif cat == "brak":
                    councilors_data[nm2]["votes_brak"] += 1
                elif cat == "za":
                    councilors_data[nm2]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm2]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm2]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm2 in names:
                councillor_sess[nm2].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
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
            for nm2 in v["named_votes"].get(cat, []):
                vectors[nm2][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, "NZ"),
                      "club_b": club_assign.get(b, "NZ"),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    data = {"generated": datetime.now().isoformat(),
            "default_kadencja": KADENCJA_ID, "kadencje": [kad]}
    return data, total_votes, total_sessions


def build_profiles(records, club_assign, roles_map):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0,
                              "nieobecny": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    profiles = []
    names = set(cv.keys())
    for rec in records:
        if rec["date"] and rec["date"] >= KAD_START:
            names.update(rec.get("nieobecni", []))
    for nm in sorted(names):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"),
                             "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": vd["brak"], "votes_nieobecny": vd["nieobecny"],
                             "votes_total": total, "rebellion_count": 0,
                             "rebellions": [], "roles": [roles_map[nm]] if roles_map.get(nm) else [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--limit", type=int, default=0, help="limit vote details (0=all) — tests only")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else (Path(args.work_dir) if args.work_dir else city_dir / "work")
    cache.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = dict(cfg.get("club_assignments", {}) or {})

    print("[lidzbark] (msesja) listing votes…")
    list_meta = list_rows_meta(cache)
    print(f"[lidzbark] {len(list_meta)} votes listed on /wyniki-glosowan")
    ids = sorted(int(k) for k in list_meta.keys())
    if args.limit:
        ids = ids[-args.limit:]
    sessions_info = parse_sessions(cache)
    print(f"[lidzbark] {len(sessions_info)} sessions on /sesje")
    roster = parse_roster(cache)
    print(f"[lidzbark] {len(roster)} councilors on /sklad-rady")
    roles_map = {k: v.get("role", "") for k, v in roster.items()}

    records = []
    fails = []
    anulowane_n = 0
    for vid in ids:
        try:
            det = parse_vote_detail(vid, cache)
        except Exception as e:
            fails.append((vid, repr(e)[:100]))
            continue
        if det is None:
            fails.append((vid, "no votingShow"))
            continue
        meta = list_meta.get(str(vid), {})
        rec = {"vid": vid, "date": meta.get("date"), "time": meta.get("time"),
               "topic": meta.get("topic", ""), "session_num": meta.get("session_num"),
               "agg": meta.get("agg", {"za": 0, "przeciw": 0, "wstrzymal": 0}),
               "blocks": det["blocks"], "nie_zaglosowali": det.get("nie_zaglosowali", []),
               "nieobecni": det.get("nieobecni", [])}
        rec["named"] = reconcile(rec)
        ok, msg = validate(rec, rec["named"])
        if not ok:
            fails.append((vid, msg))
        if det.get("anulowane"):
            anulowane_n += 1
        records.append(rec)
    valid = [r for r in records if validate(r, r["named"])[0]]
    print(f"[lidzbark] parsed {len(records)} vote details, reconciled OK {len(valid)}/{len(records)}"
          + (f", FAIL: {fails[:5]}" if fails else "") + f", anulowane={anulowane_n}")
    records = valid
    data, total_votes, total_sessions = build_output(records, club_assign, roles_map, sessions_info)
    profiles = build_profiles(records, club_assign, roles_map)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(data["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    # data.json = metadata wrapper (SPA format)
    meta_out = {"generated": data["generated"], "default_kadencja": KADENCJA_ID,
                "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(meta_out, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy2(city_dir / "config.json", docs / "config.json")
    print(f"[lidzbark] OUT: {total_votes} votes/{total_sessions} sessions/{profiles['total']} councilors")
    return 0


if __name__ == "__main__":
    import shutil
    raise SystemExit(main())
