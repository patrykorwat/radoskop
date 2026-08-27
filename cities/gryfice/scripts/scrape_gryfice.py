#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Gryfice — imienne głosowania Rady Miejskiej w Gryficach (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Gryficach na platformie AlfaTV "System Rada"
(bip.gminagryfice.pl). Rada Miejska → "Uchwały i imienne protokoły głosowań"
(/artykul/uchwaly-i-imienne-protokoly-glosowan) → per-rok → per-sesja →
per-uchwała strona z pełnym SSTRUKTURALIZOWANYM głosowaniem imiennym (czysty HTML,
bez OCR/PDF/JS):
    Wyniki głosowania: Głosowało N radnych, ZA - x, WSTRZYMUJĄCY (SIĘ) - y, PRZECIW - z
    L.p. | Nazwisko i imię | Oddany głos   (ZA / PRZECIW / WSTRZYMUJĄCY SIĘ / "-")

Każda uchwała = jedno głosowanie imienne (item/temat = numer + tytuł uchwały).

Nazwiska źródło podaje jako "Nazwisko Imię [drugie]"; konwencja Radoskopa =
"Imię Nazwisko" -> pierwszy token (nazwisko) przenoszony na koniec
(zgodnie z precedentem miedzyrzecz).

Uwaga ws. agregatów: tabela imienna ZAWSZE pokrywa cały skład Rady (każdy radny
ma przypisany ZA/PRZECIW/WSTRZYMUJĄCY SIĘ lub "-"). W ~3.5% przypadków skrót
"Głosowało N" w źródle jest niespójny z tabelą (błąd urzędnika w podsumowaniu);
zawieramy WYŁĄCZNIE tabelę imienną (autorytatywna), agregat służy tylko jako
log walidacyjny.

Użycie:
    python scrape_gryfice.py --output docs/data.json --profiles docs/profiles.json
                             [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.gminagryfice.pl"
BASE_ART = f"{BIP}/artykul/uchwaly-i-imienne-protokoly-glosowan"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

VOTE_MAP = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĄCY SIĘ": "wstrzymal_sie",
    "WSTRZYMUJĄCY": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie",
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


def slug_session_number(slug):
    m = re.search(r"sesja(?:[- ]?nr)?[- ]*([ivxlcdm]+)", slug.lower())
    if not m:
        return None
    rn = m.group(1).upper()
    val = 0
    prev = 0
    for ch in reversed(rn):
        cur = _ROMAN[ch]
        val = val - cur if cur < prev else val + cur
        prev = cur
    return val


def slug_date(slug):
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", slug)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def sessions_for_year(year):
    """List sessions in a year category /artykul/uchwaly-YYYY-r."""
    t = get(f"{BIP}/artykul/uchwaly-{year}-r")
    if not t:
        return []
    slugs = sorted(set(re.findall(r"/artykul/sesja[^\"']+", t)))
    out = []
    for h in slugs:
        if "uchwaly" in h:
            continue
        d = slug_date(h)
        num = slug_session_number(h)
        out.append({"url": BIP + h, "slug": h, "date": d, "num": num, "year": year})
    return out


def parse_aggregate(el_text):
    m = re.search(r"Głosowało\s+(\d+)[^,]*?,?\s*ZA\s*[–-]?\s*(\d+)", el_text)
    mp = re.search(r"PRZECIW\s*[–-]?\s*(\d+)", el_text)
    mw = re.search(r"WSTRZYMUJĄCY(?:\s+SIĘ)?\s*[–-]?\s*(\d+)", el_text)
    if m and mp and mw:
        return (int(m.group(1)), int(m.group(2)), int(mp.group(1)), int(mw.group(1)))
    return None


def parse_vote_page(html):
    s = BeautifulSoup(html, "lxml")
    el = s.find(string=lambda x: x and "Wyniki" in x and "głosowania" in x)
    agg = parse_aggregate(el) if el else None
    votes = []
    tbl = el.parent.find_next("table") if el else None
    if tbl:
        for tr in tbl.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) >= 3 and tds[1] and tds[1] != "Nazwisko i imię":
                name = tds[1].strip()
                vote = tds[-1].strip()
                if name:
                    votes.append((name, vote))
    return agg, votes


def reverse_name(name):
    """'Nazwisko Imię [drugie]' -> 'Imię [drugie] Nazwisko' (konwencja Radoskopa)."""
    toks = name.split()
    if len(toks) >= 2:
        return " ".join(toks[1:] + [toks[0]])
    return name


def collect_all():
    sessions = []
    for y in (2024, 2025, 2026):
        sessions += sessions_for_year(y)
    uniq = {}
    for s in sessions:
        uniq[s["url"]] = s
    sessions = list(uniq.values())
    ix = [s for s in sessions if s["date"] and s["date"] >= KAD_START]
    ix.sort(key=lambda s: s["date"])

    records = []

    def proc(sess):
        t = get(sess["url"])
        recs = []
        if not t:
            return sess, []
        s = BeautifulSoup(t, "lxml")
        seen = set()
        for a in s.find_all("a", href=True):
            href = a["href"]
            if "/artykul/uchwala-" not in href:
                continue
            topic = a.get_text(" ", strip=True)
            if not topic.startswith("Uchwa"):
                continue
            if href in seen:
                continue
            seen.add(href)
            u = (BIP + href) if href.startswith("/") else href
            tu = get(u)
            if not tu:
                continue
            agg, votes = parse_vote_page(tu)
            recs.append({
                "topic": topic.strip(), "agg": agg, "votes": votes,
                "url": u, "session_num": sess["num"],
            })
        return sess, recs

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        for sess, recs in ex.map(proc, ix):
            for r in recs:
                r["session_date"] = sess["date"]
                r["session_year"] = sess["year"]
                records.append(r)
    return records


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    validated = {"ok": 0, "mismatch": 0, "noagg": 0, "emptytable": 0}

    for rec in records:
        d = rec["session_date"]
        if not d or d < KAD_START:
            continue
        if not rec["votes"]:
            validated["emptytable"] += 1
            continue
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": []}
        for name_raw, v in rec["votes"]:
            name = reverse_name(name_raw)
            key = VOTE_MAP.get(v, "brak")
            named[key].append(name)
        # dedupe within each list (in case of doubled rows)
        for k in named:
            named[k] = list(dict.fromkeys(named[k]))

        za = len(named["za"]); pr = len(named["przeciw"]); wz = len(named["wstrzymal_sie"])
        voted = za + pr + wz
        if rec["agg"]:
            n_total, n_za, n_pr, n_wz = rec["agg"]
            if za == n_za and pr == n_pr and wz == n_wz:
                validated["ok"] += 1
            else:
                validated["mismatch"] += 1
        else:
            validated["noagg"] += 1

        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": str(rec["session_num"] or ""),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for k in ("za", "przeciw", "wstrzymal_sie", "brak"):
            sessions_by_date[d]["attendees"].update(named[k])
        all_votes.append({
            "id": str(vid), "session_date": d,
            "session_number": str(rec["session_num"] or ""),
            "topic": rec["topic"], "named_votes": named,
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
                if cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    c["votes_wstrzymal"] += 1
                else:
                    c["votes_brak"] += 1

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
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(councillor_sess[c["name"]]) / total_sessions * 100 if total_sessions else 0
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


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if not rec.get("votes"):
            continue
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak": []}
        for name_raw, v in rec["votes"]:
            name = reverse_name(name_raw)
            key = VOTE_MAP.get(v, "brak")
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

    records = collect_all()
    output, validated = build_output(records)
    k = output["kadencje"][0]
    print(f"[gryfice] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, "
          f"radni: {k['total_councilors']}")
    print(f"[gryfice] walidacja agregatow: ok={validated['ok']} mismatch={validated['mismatch']} "
          f"noagg={validated['noagg']} emptytable={validated['emptytable']}")
    if validated["mismatch"] or validated["noagg"]:
        print(f"[gryfice] UWAGA: {validated['mismatch']} agregatow niespojnych z tabela imienna "
              f"(zrodlo); tabela imienna autorytatywna; {validated['noagg']} bez agregatu w zrodle")
    profiles = build_profiles(records)
    save_split(output, Path(args.output), profiles)
    print("[gryfice] OK")


if __name__ == "__main__":
    main()
