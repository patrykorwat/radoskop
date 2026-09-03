#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Nowa Sól — imienne głosowania Rady Miejskiej (IX kadencja).

Źródło: portal-posiedzenia.pl (System Posiedzenia), subdomena `nowasol`
(BIP bip.nowasol.pl -> link 'Wykaz imiennych głosowań radnych').
Adapter: radoskop/scripts/lib_posiedzenia_pl.py.
Głosowania jawne imienne per punkt sesji; partie/kluby z chart API.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from lib_posiedzenia_pl import (PosiedzeniaClient, classify_sessions,  # noqa: E402
                                session_num, votes_from_chart)

SUBDOMENA = "nowasol"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    args = ap.parse_args()
    city = Path(args.city_dir)
    docs = city / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    c = PosiedzeniaClient(SUBDOMENA)
    d = c.login()
    print(f"[{SUBDOMENA}] podmiot={c.podmiot} {c.nazwa}", flush=True)
    sess = classify_sessions(c.sessions(), KAD_START)
    print(f"[{SUBDOMENA}] sesje IX: {len(sess)}", flush=True)

    all_votes = []
    clubs = {}
    seen_names = set()
    for s in sess:
        date = s["date"][:10]
        num = session_num(s["name"])
        for p in s["points"]:
            ch = c.vote_detail(p["id"])
            res = votes_from_chart(ch, None) if ch else None
            if res is None:
                continue
            named, nieob, vclubs = res
            if not any(named.values()) and not nieob:
                continue
            for nm, cl in vclubs.items():
                clubs[nm] = cl
            for nm in list(named["za"]) + list(named["przeciw"]) + \
                    list(named["wstrzymal_sie"]) + nieob:
                seen_names.add(nm)
            all_votes.append({"date": date, "session_num": num,
                              "topic": (p.get("name") or "").strip(),
                              "ts": ch.get("startDateTime", ""),
                              "za": named["za"], "przeciw": named["przeciw"],
                              "wstrzymal_sie": named["wstrzymal_sie"],
                              "nieobecni_glos": nieob})
    print(f"[{SUBDOMENA}] głosowania: {len(all_votes)} radni: {len(seen_names)}",
          flush=True)
    if len(all_votes) < 20:
        raise SystemExit(f"ZA MAŁO głosów ({len(all_votes)}) — przerywam")

    # ---- build (format jak zabki/lib_esesja) ----
    councilors_seen = sorted(seen_names)
    all_votes.sort(key=lambda v: (v["date"], v.get("ts", "")))
    sessions_data = []
    by_sess = defaultdict(list)
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess[v["date"]].append(v)
    for dd, vs in sorted(by_sess.items()):
        sessions_data.append({"date": dd, "number": dd,
                              "label": f"Sesja {vs[0].get('session_num','')} ({dd})",
                              "vote_count": len(vs)})
    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"],
              "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": v.get("session_num", ""),
                          "topic": v["topic"], "named_votes": nv,
                          "counts": {"for_": len(v["za"]),
                                     "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v.get("nieobecni_glos", []))}})
    total_votes = len(votes_out)
    total_sessions = len(sessions_data)
    cdata = {n: {"name": n, "club": clubs.get(n, ""), "votes_za": 0,
                 "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
                 "votes_nieobecny": 0} for n in councilors_seen}
    csess = defaultdict(set)
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                         ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in cdata:
                    cdata[nm][key] += 1
                    csess[nm].add(v["session_date"])
    councilors_list = []
    for cc in cdata.values():
        present = cc["votes_za"] + cc["votes_przeciw"] + cc["votes_wstrzymal"]
        councilors_list.append({
            "name": cc["name"], "club": cc["club"], "district": None,
            "frekwencja": round((len(csess.get(cc["name"], set())) / total_sessions * 100) if total_sessions else 0, 1),
            "aktywnosc": round((present / total_votes * 100) if total_votes else 0, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
            "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
            "votes_nieobecny": cc["votes_nieobecny"],
            "votes_total": present + cc["votes_nieobecny"],
            "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    club_counts = defaultdict(int)
    for cc in councilors_list:
        if cc["club"]:
            club_counts[cc["club"]] += 1
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(club_counts), "sessions": sessions_data,
           "total_sessions": total_sessions, "total_votes": total_votes,
           "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    def slugify(nm):
        import unicodedata
        s = unicodedata.normalize("NFKD", nm.lower())
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip().replace(" ", "-")
    profiles = {"profiles": [{"name": cc["name"], "slug": slugify(cc["name"]),
                              "kadencje": {KADENCJA_ID: {
                                  "club": cc["club"], "has_voting_data": True,
                                  "has_activity_data": False,
                                  "frekwencja": cc["frekwencja"],
                                  "aktywnosc": cc["aktywnosc"],
                                  "zgodnosc_z_klubem": 0.0,
                                  "votes_za": cc["votes_za"],
                                  "votes_przeciw": cc["votes_przeciw"],
                                  "votes_wstrzymal": cc["votes_wstrzymal"],
                                  "votes_brak": cc["votes_brak"],
                                  "votes_nieobecny": cc["votes_nieobecny"],
                                  "votes_total": cc["votes_total"],
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": [], "notes": "",
                                  "former": False, "mid_term": False}}}
                             for cc in councilors_list],
               "total": len(councilors_list)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{SUBDOMENA}] ZAPISANO: {total_sessions} sesji, {total_votes} głosowań, "
          f"{len(councilors_list)} radnych", flush=True)


if __name__ == "__main__":
    main()
