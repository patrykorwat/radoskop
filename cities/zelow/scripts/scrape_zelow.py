#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Zelow — imienne głosowania Rady Miejskiej w Miastku (AlfaTV "System Rada").

Źródło: https://rada.zelow.pl/ (AlfaTV System Rada, IX kadencja 2024-2029).
Pełne per-radny głosowania serwer-renderowane jako HTML (bez PDF/OCR).

Struktura:
  /glosowania                -> lista sesji (Nazwa | data | Liczba głosowań | link)
  /glosowania/posiedzenie/{id} -> wszystkie głosowania 1 sesji w HTML; każdy blok
      to div.accordion-item z: temat + decyzja (Przyjęto/Odrzucono), wierszem
      agregatów oraz tabelą "Imienny wykaz głosowania" (Imię i nazwisko | Głos).
  /sklad-rady                 -> roster radnych (linki /sklad-rady/radny/{id})
  /sklad-rady/radny/{id}      -> nazwisko (kluby NIE publikowane u Miastka)

34 sesje IX kadencji (2024-05 .. 2026-08), 16 radnych.
Adapter rada.zelow.pl == rada.miastoturek.pl (ten sam platformowy szablon AlfaTV).

Użycie:
    python scrape_zelow.py --city-dir cities/zelow [--cache-dir .cache]
"""

import argparse
import hashlib
import html as _html
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://rada.zelow.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.35
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=60, verify=False)
    resp.raise_for_status()
    if cache_dir is not None:
        cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.text


# ---------------------------------------------------------------------------
# 1. Sesje
# ---------------------------------------------------------------------------
def discover_sessions(cache_dir):
    html = fetch(f"{BASE}/glosowania", cache_dir)
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=re.compile(r"/glosowania/posiedzenie/\d+")):
        row = a.find_parent("tr") or a.find_parent("div")
        txt = re.sub(r"\s+", " ", row.get_text(" ", strip=True)) if row else ""
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
        cm = re.search(r"Sesj\S+\s+Rady Miejskiej\S*\s+(.*?)\s+\d{4}-\d{2}-\d{2}", txt)
        mid = int(re.search(r"(\d+)$", a["href"]).group(1))
        out.append({"id": mid, "name": (cm.group(1).strip() if cm else ""),
                    "date": dm.group(1) if dm else ""})
    # dedupe
    seen = set(); uniq = []
    for s in out:
        if s["id"] in seen:
            continue
        seen.add(s["id"]); uniq.append(s)
    uniq.sort(key=lambda s: s["date"])
    return uniq


# ---------------------------------------------------------------------------
# 2. Głosowania sesji
# ---------------------------------------------------------------------------
def parse_session_votes(html):
    soup = BeautifulSoup(html, "html.parser")
    votes = []
    for item in soup.find_all("div", class_="accordion-item"):
        tables = item.find_all("table")
        big = [tb for tb in tables if len(tb.find_all("tr")) > 15]
        if not big:
            continue
        tbl = big[0]
        rows = []
        for tr in tbl.find_all("tr"):
            tds = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()
                   for td in tr.find_all("td")]
            tds = [t for t in tds if t]
            if len(tds) >= 2 and not re.match(r"^\d+$", tds[0]):
                rows.append((tds[0], tds[1].lower()))
        itext = item.get_text(" ", strip=True)
        # topic + decision: text before "Zakończono:"
        pre, _sep, _post = itext.partition("Zakończono:")
        dm = re.findall(r"(Przyjęto|Odrzucono)", pre)
        decision = dm[-1] if dm else ""
        topic = re.sub(r"\s*(Przyjęto|Odrzucono)\s*$", "", pre).strip()
        named = defaultdict(list)
        for nm, vt in rows:
            if vt == "za":
                named["za"].append(nm)
            elif vt == "przeciw":
                named["przeciw"].append(nm)
            elif "wstrzym" in vt:
                named["wstrzymal_sie"].append(nm)
        votes.append({"topic": topic, "decision": decision,
                      "named": {k: list(v) for k, v in named.items()}})
    return votes


# ---------------------------------------------------------------------------
# 3. Roster + kluby
# ---------------------------------------------------------------------------
def fetch_roster(cache_dir):
    html = fetch(f"{BASE}/sklad-rady", cache_dir)
    links = sorted(set(re.findall(r"href=\"(/sklad-rady/radny/\d+)\"", html)),
                   key=lambda x: int(re.search(r"(\d+)$", x).group(1)))
    roster = {}
    for l in links:
        try:
            ph = fetch(f"{BASE}{l}", cache_dir)
            soup = BeautifulSoup(ph, "html.parser")
            tm = re.search(r"<title>(.*?)</title>", ph, re.S)
            name = (tm.group(1).strip().split(" | ")[0].strip()) if tm else ""
            club = ""
            for dt in soup.find_all("dt"):
                if "Przynależność" in dt.get_text(" ", strip=True):
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        club = dd.get_text(" ", strip=True).strip()
                    break
            if name:
                roster[l] = {"name": _html.unescape(name), "club": club}
        except Exception:
            continue
    return roster


# ---------------------------------------------------------------------------
# 4. Output builders (wzorzec scrape_police / scrape_klodzko)
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_key(name, roster):
    # Zelow: System Rada nie publikuje klubów (Przynależność brak na stronach
    # /sklad-rady/radny/{id}) -> club_assignments PENDING, clubs {}.
    # Zwracamy surową nazwę klubu z radny-page (zwykle ""), NIGDY nie fabrykujemy.
    return roster.get(name, {}).get("club", "") or ""


def build_output(records, roster):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
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

    # canonical roster from source (radny pages) — union with names appearing in votes
    roster_names = set(r["name"] for r in roster.values())
    all_names |= roster_names

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": _club_key(name, roster) or "",
                                 "district": None, "votes_za": 0, "votes_przeciw": 0,
                                 "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0,
                                 "rebellions": []}
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
        pairs.append({"a": a, "b": b, "club_a": _club_key(a, roster) or "",
                      "club_b": _club_key(b, roster) or "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_key(n, roster) or "" for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records, roster):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["session_date"] for r in records if r["session_date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len([1 for r in records if r["session_date"] >= KAD_START])) * 100
        profiles.append({
            "name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {
                "club": _club_key(nm, roster) or "", "has_voting_data": True,
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
    print(f"[zelow] sesje: {len(sessions)}")
    roster = fetch_roster(cache)
    # convert link-keyed -> name-keyed for club lookup
    roster_by_name = {v["name"]: v for v in roster.values()}
    print(f"[zelow] roster: {len(roster)}")
    for r in sorted(roster.values(), key=lambda x: x["name"]):
        print(f"   {r['name']:<28} {r['club']}")

    records = []
    for s in sessions:
        try:
            html = fetch(f"{BASE}/glosowania/posiedzenie/{s['id']}", cache)
            vs = parse_session_votes(html)
            for v in vs:
                records.append({"session_date": s["date"], "session_num": s["name"][:20],
                                "topic": v["topic"], "named": v["named"]})
            print(f"  {s['date']} {s['name'][:15]:<16} votes={len(vs)}")
        except Exception as e:
            print(f"  [ERR {s['id']}] {type(e).__name__}: {e}")

    output = build_output(records, roster_by_name)
    profiles = build_profiles(records, roster_by_name)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    print(f"[zelow] total votes={output['kadencje'][0]['total_votes']} "
          f"sessions={output['kadencje'][0]['total_sessions']} "
          f"councilors={output['kadencje'][0]['total_councilors']}")


if __name__ == "__main__":
    main()
