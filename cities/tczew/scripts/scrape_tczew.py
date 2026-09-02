#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Tczew — imienne głosowania Rady Miejskiej w Tczewie.

Źródło: https://www.sesje.tczew.pl/wyniki/ — "Elektroniczny rejestr głosowań
Rady Miejskiej w Tczewie" (custom PHP, server-renderowany HTML, UTF-8 BOM).

Struktura:
  ?p=sesje                          -> lista sesji; linki module/pdf/?hash=H&n=NUM/RRRR-DD.MM.RRRR
  module/sesja-protokol.php?hash=H  -> pełny protokół: lista radnych (numerowana)
       + powtarzające się bloki:
         <h6>Wyniki głosowania jawnego, imiennego</h6><h4>TEMAT</h4><table>
         wiersze: ZA głosowali | PRZECIW głosowali | WSTRZYMALI SIĘ | Nieobecni na sesji | Nie głosowali
         kolumna 3: "1) Nazwisko Imię, 2) ..." lub "- brak -"

IX kadencja: 29 sesji (I/2024-07.05.2024 .. XXIX/2026-09.07.2026), 23 radnych.

Użycie:
    python3 scrape_tczew.py --city-dir cities/tczew [--cache-dir .cache]
"""

import argparse
import hashlib
import json
import re
import ssl
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://www.sesje.tczew.pl/wyniki"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.4
_LAST_REQ = 0.0

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _rate():
    global _LAST_REQ
    d = time.time() - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None) -> str:
    if cache_dir is not None:
        cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    raw = urlopen(req, timeout=60, context=_CTX).read()
    txt = raw.decode("utf-8-sig", errors="replace")
    if cache_dir is not None:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(txt, encoding="utf-8", errors="ignore")
    return txt


def parse_date_from_name(n: str) -> str:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", n)
    if not m:
        return ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def discover_sessions(cache_dir=None):
    t = fetch(BASE + "/?p=sesje", cache_dir)
    seen = {}
    for h, n in re.findall(r'module/pdf/\?hash=([0-9a-f]+)&n=([^"&]+)"', t):
        if h in seen:
            continue
        d = parse_date_from_name(n)
        num = n.split("/")[0]
        seen[h] = {"hash": h, "date": d, "num": num}
    return sorted(seen.values(), key=lambda x: x["date"])


NAME_ROW_RE = re.compile(r"^\d+\.\s*([A-ZŁŚŃŹŻĆÓ][\wŁŁŚŃŹŻĆÓąęłńóźżść]+(?:\s+[A-ZŁŚŃŹŻĆÓąęłńóźżść]+)+)", re.M)


def _fn(nm: str) -> str:
    """Platforma publikuje 'Nazwisko Imię' (jak BIP 'Nazwisko i imię') — zamień
    na porządek 'Imię Nazwisko' (reguła verify_city councilor_names).
    Ostatni token = imię; poprzednie = nazwisko(a) (joint allowed)."""
    parts = nm.split()
    if len(parts) < 2:
        return nm
    return " ".join(parts[1:]) + " " + parts[0]


def parse_protocol(html: str):
    """-> (roster_names list, votes list[{topic, named{za,przeciw,wstrzymal_sie}, absent}])"""
    roster = []
    # roster = longest numbered name list (appears at top / lista obecności)
    best = []
    for m in re.finditer(r"<tr><td>(\d+)\.\s*</td><td>([^<]+)</td>", html):
        nm = _fn(re.sub(r"\s+", " ", m.group(2)).strip())
        if nm and nm not in best:
            best.append(nm)
        if m.group(1) == "1":
            if len(best) > len(roster):
                roster = best
            best = [nm]
    if len(best) > len(roster):
        roster = best

    votes = []
    # split into vote blocks
    parts = re.split(r"<h6>\s*Wyniki głosowania jawnego, imiennego\s*</h6>", html)
    for seg in parts[1:]:
        tm = re.search(r"<h4>\s*(.*?)\s*</h4>", seg, re.S)
        topic = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tm.group(1))).strip() if tm else ""
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        absent = []
        tbl = re.search(r"<table.*?</table>", seg, re.S)
        if not tbl:
            continue
        for rlabel, rcount, rnames in re.findall(
            r"<tr><td[^>]*nowrap>(.*?)</td><td[^>]*>(.*?)</td><td[^>]*>(.*?)</td></tr>",
            tbl.group(0), re.S,
        ):
            lab = re.sub(r"<[^>]+>", "", rlabel).strip().upper()
            cell = re.sub(r"<[^>]+>", " ", rnames)
            cell = re.sub(r"\s+", " ", cell).strip()
            names = []
            if cell and "brak" not in cell.lower():
                for pn in re.split(r",\s(?=\d+\))", cell):
                    pn = re.sub(r"^\s*\d+\)\s*", "", pn).strip()
                    pn = re.sub(r"\s*\d+\)\s*$", "", pn).strip()
                    if pn and "brak" not in pn.lower():
                        names.append(_fn(pn))
            if lab.startswith("ZA"):
                named["za"] = names
            elif lab.startswith("PRZECIW"):
                named["przeciw"] = names
            elif lab.startswith("WSTRZYMALI"):
                named["wstrzymal_sie"] = names
            elif lab.startswith("NIEOBECNI"):
                absent = names
        total = sum(len(v) for v in named.values())
        if total == 0 and not absent:
            continue
        votes.append({"topic": topic, "named": named, "absent": absent})
    return roster, votes


# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_key(name, roster):
    return ""  # kluby PENDING (brak na platformie; kuratorować z BIP)


def build_output(records, roster_names):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        sessions_by_date[d]["attendees"].update(rec.get("absent", []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
            "topic": rec.get("topic", ""), "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
            "speakers": [],
        })

    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    all_names.update(roster_names)

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": "", "district": None,
                                 "votes_za": 0, "votes_przeciw": 0,
                                 "votes_wstrzymal": 0, "votes_brak": 0,
                                 "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
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

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records, roster_names):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    sess_votes = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        sess_votes.setdefault(d, []).append(rec)
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    for nm in roster_names:
        cv[nm]  # ensure present
    profiles = []
    n_sessions = len(sess_votes) or 1
    total_vote_cnt = sum(len(v) for v in sess_votes.values()) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / total_vote_cnt * 100
        profiles.append({
            "name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1),
                "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": output.get("generated", ""),
                   "default_kadencja": output.get("default_kadencja", ""),
                   "kadencje": stubs}, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    sessions = [s for s in sessions if s["date"] >= KAD_START]
    print(f"[tczew] sesje IX: {len(sessions)}")

    records = []
    roster_names = []
    for s in sessions:
        try:
            html = fetch(f"{BASE}/module/sesja-protokol.php?hash={s['hash']}", cache)
            roster, votes = parse_protocol(html)
            if len(roster) > len(roster_names):
                roster_names = roster
            for v in votes:
                records.append({"session_date": s["date"], "session_num": s["num"],
                                "topic": v["topic"], "named": v["named"],
                                "absent": v.get("absent", [])})
            print(f"  {s['date']} {s['num']:<8} votes={len(votes)} roster={len(roster)}")
        except Exception as e:
            print(f"  [ERR {s['hash']}] {type(e).__name__}: {e}")

    print(f"[tczew] roster: {len(roster_names)}")
    output = build_output(records, roster_names)
    profiles = build_profiles(records, roster_names)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[tczew] total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")


if __name__ == "__main__":
    main()
