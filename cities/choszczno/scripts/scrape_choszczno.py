#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Choszczno — imienne głosowania Rady Miejskiej w Choszcznie (IX kadencja 2024-2029).

Źródło: AlfaTV "System Rada" (rada.choszczno.pl), czysty strukturalny HTML (bez OCR/PDF/JS).

Struktura:
- /glosowania — tabela sesji IX kad. "Posiedzenie Data liczba głosowań Szczegóły":
      I sesja 2024-05-07 14:00 10 [Sprawdź wyniki głosowań -> /glosowania/posiedzenie/{id}]
- /glosowania/posiedzenie/{id} — vote Accordion, jeden accordion-item per głosowanie:
      header: <span class="w-100">- druk nr N …;</span> + <span class="badge">Przyjęto/…</span>
      body:   <p>Zakończono: … Większość: …</p>
              tabela zbiorcza: Głosy za | Głosy wstrzymujące | Głosy przeciw | Głosy nieoddane | Nieobecni
              <p>Imienny wykaz głosowania</p>
              tabela imienna: "Imię i nazwisko | Głos" (za / przeciw / wstrzymał się / nieoddany / nieobecny)

Nazwiska źródło podaje jako "Imię Nazwisko" (ten sam porządek co w tabeli głosowań) — zgodne
z konwencją Radoskopa, brak odwracania.

Rozmiar Rady = 14 radnych (skład z /sklad-rady; +1 wygaszony mandat Anetta Bikowska pominięta).
Agregat w źródle (tabela zbiorcza) walidacyjnie porównywany z tabelą imienną.

Użycie:
    python scrape_choszczno.py --output docs/data.json --profiles docs/profiles.json
"""

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://rada.choszczno.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

VOTE_MAP = {
    "za": "za",
    "przeciw": "przeciw",
    "wstrzymał się": "wstrzymal_sie",
    "nieoddany": "brak",
    "nieobecny": "nieobecny",
}


def _normname(s):
    s = s.lower()
    table = str.maketrans(
        {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z"}
    )
    s = s.translate(table)
    return re.sub(r"[^a-z0-9]", "", s)


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    sn = str(name or "").lower()
    for pl, a in repl.items():
        sn = sn.replace(pl, a)
    sn = re.sub(r"[^a-z0-9]+", "-", sn)
    return sn.strip("-")


def get(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=40, verify=False)
            if r.status_code == 200:
                return r.text
        except Exception:
            time.sleep(1 + i)
    return None


_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_int(rn):
    rn = rn.upper()
    val = 0
    prev = 0
    for ch in reversed(rn):
        cur = _ROMAN[ch]
        val = val - cur if cur < prev else val + cur
        prev = cur
    return val


def list_sessions():
    """Session list z /glosowania: (num, date ISO, posiedzenie_id)."""
    t = get(f"{BIP}/glosowania")
    if not t:
        return []
    soup = BeautifulSoup(t, "lxml")
    tbl = soup.find("table")
    if not tbl:
        return []
    sessions = []
    for tr in tbl.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 3:
            continue
        # tds[0]='I sesja', tds[1]='2024-05-07 14:00', tds[2]='10'
        mnum = re.match(r"^([ivxlcdm]+)\s+sesja", tds[0].lower())
        if not mnum:
            continue
        a = tr.find("a", href=True)
        if not a or "posiedzenie" not in a["href"]:
            continue
        mdate = re.match(r"(\d{4}-\d{2}-\d{2})", tds[1])
        if not mdate:
            continue
        pid = a["href"].rsplit("/", 1)[-1]
        sessions.append({
            "num": roman_int(mnum.group(1)),
            "date": mdate.group(1),
            "posiedzenie_id": pid,
            "link": f"{BIP}/glosowania/posiedzenie/{pid}",
        })
    return sessions


def parse_session_page(html):
    """Extract per-vote records from a /glosowania/posiedzenie/{id} page."""
    soup = BeautifulSoup(html, "lxml")
    records = []
    for item in soup.select(".accordion-item"):
        header = item.select_one(".accordion-header, h2")
        # topic from the w-100 span
        span = item.select_one("span.w-100")
        badge = item.select_one(".badge")
        topic = span.get_text(" ", strip=True) if span else ""
        status = badge.get_text(" ", strip=True) if badge else ""
        # aggregate table (first table in body: Głosy za/wstrzymujące/przeciw/nieoddane/Nieobecni)
        agg = None
        votes = []
        body = item.select_one(".accordion-body")
        if body:
            tables = body.find_all("table")
            if tables:
                agg_body = tables[0].find("tbody")
                if agg_body:
                    cnts = [td.get_text(" ", strip=True) for td in agg_body.find("tr").find_all("td")]
                    if len(cnts) >= 5:
                        try:
                            agg = tuple(int(c) for c in cnts[:5])
                        except ValueError:
                            agg = None
            # imienny table = the one whose thead contains "Imię i nazwisko"
            for tbl in tables:
                ths = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
                if "Imię" in " ".join(ths):
                    for tr in tbl.find_all("tr"):
                        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                        if len(tds) >= 2 and tds[0] and tds[1]:
                            votes.append((tds[0], tds[1]))
                    break
        records.append({"topic": topic, "status": status, "agg": agg, "votes": votes})
    return records


def collect_all():
    sessions = list_sessions()
    ix = [s for s in sessions if s["date"] >= KAD_START]
    ix.sort(key=lambda s: s["date"])
    records_by_session = []

    from concurrent.futures import ThreadPoolExecutor

    def proc(sess):
        t = get(sess["link"])
        if not t:
            return sess, []
        recs = parse_session_page(t)
        return sess, recs

    with ThreadPoolExecutor(max_workers=3) as ex:
        for sess, recs in ex.map(proc, ix):
            for r in recs:
                r["session_date"] = sess["date"]
                r["session_num"] = sess["num"]
            records_by_session.append((sess, recs))
    return records_by_session


def build_output(records_by_session):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    validated = {"ok": 0, "mismatch": 0, "noagg": 0, "emptytable": 0}

    for sess, recs in records_by_session:
        d = sess["date"]
        for rec in recs:
            if not rec["votes"]:
                validated["emptytable"] += 1
                continue
            named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": [], "nieobecny": []}
            for name, v in rec["votes"]:
                key = VOTE_MAP.get(v.lower().strip(), "brak")
                named[key].append(name)
            for k in named:
                named[k] = list(dict.fromkeys(named[k]))
            za = len(named["za"]); pr = len(named["przeciw"]); wz = len(named["wstrzymal_sie"])
            if rec["agg"]:
                n_za, n_wz, n_pr, n_brak, n_nie = rec["agg"]
                if za == n_za and pr == n_pr and wz == n_wz:
                    validated["ok"] += 1
                else:
                    validated["mismatch"] += 1
            else:
                validated["noagg"] += 1

            if d not in sessions_by_date:
                sessions_by_date[d] = {"date": d, "number": str(sess["num"]),
                                       "vote_count": 0, "attendees": set()}
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            for k in named:
                sessions_by_date[d]["attendees"].update(named[k])
            all_votes.append({
                "id": str(vid), "session_date": d,
                "session_number": str(sess["num"]),
                "topic": rec["topic"], "status": rec.get("status", ""),
                "named_votes": named,
                "counts": {"za": za, "przeciw": pr, "wstrzymal_sie": wz},
            })

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

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": "",
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                if name not in councilors_data:
                    continue
                c = councilors_data[name]
                c["votes_" + {"za": "za", "przeciw": "przeciw", "wstrzymal_sie": "wstrzymal",
                              "brak": "brak", "nieobecny": "nieobecny"}[cat]] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for n in names:
                councillor_sess[n].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        active_sess = len(councillor_sess[c["name"]])
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = active_sess / total_sessions * 100 if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"],
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
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
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
    club_counts = Counter()
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, validated


def build_profiles(records_by_session):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for sess, recs in records_by_session:
        d = sess["date"]
        if d < KAD_START:
            continue
        for rec in recs:
            if not rec.get("votes"):
                continue
            named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": [], "nieobecny": []}
            for name, v in rec["votes"]:
                key = VOTE_MAP.get(v.lower().strip(), "brak")
                named[key].append(name)
            for k in named:
                named[k] = list(dict.fromkeys(named[k]))
            for cat, names in named.items():
                for name in names:
                    cv[name][cat] += 1
                    cv[name]["votes"].append({"session": d, "vote": cat})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw, 1), "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
            }},
        })
    return {"profiles": profiles}


def save_split(output, out_path, profiles):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": []}
    for kad in output["kadencje"]:
        kid = kad["id"]
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
        index["kadencje"].append({"id": kad["id"], "label": kad.get("label", "")})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=".cache")
    args = ap.parse_args()

    records_by_session = collect_all()
    output, validated = build_output(records_by_session)
    k = output["kadencje"][0]
    print(f"[choszczno] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, "
          f"radni: {k['total_councilors']}")
    print(f"[choszczno] walidacja agregatow: ok={validated['ok']} mismatch={validated['mismatch']} "
          f"noagg={validated['noagg']} emptytable={validated['emptytable']}")
    if validated["mismatch"] or validated["noagg"]:
        print(f"[choszczno] UWAGA: {validated['mismatch']} agregaty niespojne z tabela imienna "
              f"(zrodlo); tabela imienna autorytatywna; {validated['noagg']} bez agregatu")
    profiles = build_profiles(records_by_session)
    save_split(output, Path(args.output), profiles)
    print("[choszczno] OK")


if __name__ == "__main__":
    main()
