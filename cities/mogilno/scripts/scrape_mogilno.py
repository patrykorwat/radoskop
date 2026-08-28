#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Mogilno — imienne głosowania Rady Miejskiej w Mogilnie (IX kadencja 2024-2029).

Źródło: BIP URZĘDU MIEJSKIEGO w Mogilnie (platforma Nefeni „Nowoczesna Gmina”,
https://bip.mogilno.pl, API załączników https://bip-api.mogilno.pl). Rada Miejska
publikuje głosowania imienne w kategorii „Rada Miejska” jako kategorie per rok
(2024-rok / 2025-rok / 2026-rok), a każda kategoria S£sesja {rzymska} z {data}”
zawiera m.in. artykuł „glosowanie-imienne” z załącznikiem PDF (format tekstowy
eSesja), zawierającym wyniki głosowań imiennych ZA / PRZECIW / WSTRZYMUJE SIĘ
per radny (tabela „UPRAWNIENI DO GŁOSOWANIA”, agregat do walidacji).

Format PDF (per głosowanie):
  {romb} sesja Rady Miejskiej w Mogilnie
  GŁOSOWANIE
  {temat}
  TYP GŁOSOWANIA Jawne imienne DATA GŁOSOWANIA 2026-08-26 10:21:30
  PRZYJĘTO (W. ZWYKŁA )
  LICZBA UPRAWNIONYCH 21 GŁOSY ZA 21
  LICZBA OBECNYCH 21 GŁOSY PRZECIW 0
  LICZBA NIEOBECNYCH 0 GŁOSY WSTRZYMUJĄCE SIĘ 0
  GŁOSY NIEODDANE 0
  KWORUM ZOSTAŁO OSIĄGNIĘTE
  UPRAWNIENI DO GŁOSOWANIA
  LP IMIĘ I NAZWISKO GŁOS LP IMIĘ I NAZWISKO GŁOS
  1 Dorota Czarnecka za 13 Maria Malczewska za
  ...

Sesja I (inauguracyjna, 2024-05-07) oraz sesje XII (2025-03-19) i XV
(2025-06-18) NIE mają artykułu glosowanie-imienne — pomijane.

Kluby radnych: BIP Mogilna NIE publikuje kategorii „Kluby radnych” — club_assignments
PENDING (brak danych do kuratorowania). Wszyscy radni traktowani jako Niezrzeszeni (NZ).

Użycie:
    python scrape_mogilno.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""

import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SITE = "https://bip.mogilno.pl"
ATT = "https://bip-api.mogilno.pl"
YEAR_CATS = ["kategorie/522-2026-rok", "kategorie/416-2025-rok", "kategorie/276-2024-rok"]
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.6
_LAST_REQ = 0.0
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"}

CLUBS_META = {
    "NZ": {"name": "Niezrzeszeni", "color": "#6b7280",
           "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}
CLUB_ASSIGN = {}  # PENDING — BIP nie publikuje klubów radnych

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(roman):
    n, prev = 0, 0
    for ch in reversed((roman or "").upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n if n else 0


def club_of(name):
    return CLUB_ASSIGN.get(name, "NZ")


def _norm(s):
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def make_slug(name):
    repl = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s",
            "ź": "z", "ż": "z", "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N",
            "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z"}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def canonical_name(raw):
    raw = raw.strip()
    return " ".join(p[:1].upper() + p[1:].lower() if p else "" for p in raw.split())


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url, cache_dir=None, binary=False, tries=6):
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".json"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    for attempt in range(tries):
        _rate()
        try:
            resp = requests.get(url, headers=UA, timeout=120)
        except Exception:
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 200:
            data = resp.content if binary else resp.text
            if cache_dir is not None:
                (cache_dir / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".json"))).write_bytes(
                    data if binary else data.encode("utf-8"))
            return data
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {tries} tries: {url}")


# ---- 1. Odkrycie sesji IX kadencji (kategorie per rok) ----
def collect_sessions(cache_dir=None):
    """Zwraca [{catid, catslug, roman, date, glos_art, attach_id}] — IX kadencja."""
    out, seen = [], set()
    for ycat in YEAR_CATS:
        html = fetch(f"{SITE}/{ycat}?lang=PL", cache_dir)
        # podkategorie = sesje
        for _full, catid, _slug in re.findall(r'href="(/kategorie/(\d+)-([a-z0-9-]+))[^"]*"', html):
            href = _full
            sm = re.search(r'sesja-([ivxlcdm]+)-', href)
            if not sm or catid in seen:
                continue
            seen.add(catid)
            roman = sm.group(1).upper()
            dm = re.search(r'-(\d{1,2})-([a-ząćęłńóśźż]+)-(\d{4})-', href)
            if not dm:
                continue
            _, month_word, year = dm.group(1), dm.group(2), dm.group(3)
            dd = int(dm.group(1))
            mm = _MONTHS.get(month_word.lower())
            if not mm:
                continue
            date = f"{year}-{mm:02d}-{dd:02d}"
            if date < KAD_START:
                continue
            # znajdź artykuł glosowanie-imienne w kategorii sesji
            sh = fetch(f"{SITE}{href}?lang=PL", cache_dir)
            gm = re.search(r'/artykuly/(\d+)-glosowanie-imienne', sh)
            if not gm:
                print(f"  [skip] {roman} {date}: brak artykułu glosowanie-imienne")
                continue
            aid = gm.group(1)
            ah = fetch(f"{SITE}{href}/artykuly/{aid}-glosowanie-imienne?lang=PL", cache_dir)
            attm = re.search(r'(https://[a-z0-9.-]*?/api/attachments/(\d+))', ah)
            att_id = attm.group(2) if attm else None
            if not att_id:
                print(f"  [warn] {roman} {date}: brak załącznika")
                continue
            out.append({"catid": catid, "catslug": href.lstrip("/"),
                        "roman": roman, "date": date, "glos_art": aid, "attach_id": att_id})
    out.sort(key=lambda s: s["date"])
    return out


_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9,
    "pazdziernika": 10, "października": 10, "listopada": 11, "grudnia": 12,
}


# ---- 2. Parsowanie PDF głosowań imiennych ----
_VOTE_TOKENS = (r"za|przeciw|wstrzymał\s+się|wstrzymal\s+sie|wstrzymujący\s+się|"
                r"nie\s+brał\s+udziału|nie\s+bral\s+udzialu|nie\s+głosował|nie\s+glosowal|nieobecny")
_PAIR_RE = re.compile(
    r"(\d+)\s+([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻŻa-ząćęłńóśźż.\- ]*?)\s+(" + _VOTE_TOKENS + r")\b")


def parse_pdf(data):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    blocks = re.split(r"\nGŁOSOWANIE\n|\nGLOSOWANIE\n", text)
    votes = []
    for b in blocks[1:]:
        dm = re.search(r"DATA GŁOSOWANIA\s+([\d\- :]+)", b)
        date = dm.group(1).strip() if dm else ""
        za = re.search(r"GŁOSY ZA\s+(\d+)", b)
        prz = re.search(r"GŁOSY PRZECIW\s+(\d+)", b)
        wst = re.search(r"GŁOSY WSTRZYMUJĄCE SIĘ\s+(\d+)", b)
        t = re.split(r"\nTYP GŁOSOWANIA", b)[0]
        title = " ".join(x.strip() for x in t.split("\n") if x.strip())
        region = b.split("UPRAWNIENI DO GŁOSOWANIA", 1)[1] if "UPRAWNIENI DO GŁOSOWANIA" in b else b
        pairs = _PAIR_RE.findall(region)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        for lp, name, tok in pairs:
            name = canonical_name(name)
            tok = tok.replace("\u0142", "l").lower()
            if tok == "za":
                named["za"].append(name)
            elif tok == "przeciw":
                named["przeciw"].append(name)
            elif "wstrzym" in tok:
                named["wstrzymal_sie"].append(name)
            elif "nieobec" in tok:
                named["nieobecni"].append(name)
            else:
                named["brak_glosu"].append(name)
        votes.append({"date": date, "title": title,
                      "agg": {"za": int(za.group(1)) if za else None,
                              "przeciw": int(prz.group(1)) if prz else None,
                              "wstrzym": int(wst.group(1)) if wst else None},
                      "named": named, "pairs_n": len(pairs)})
    return votes


# ---- 3. Kolekcja wszystkich głosowań ----
def collect_all(sessions, cache_dir=None):
    records = []
    for s in sessions:
        att_url = f"{ATT}/api/attachments/{s['attach_id']}"
        try:
            pdf = fetch(att_url, cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['roman']} {s['date']}: {e}")
            continue
        vs = parse_pdf(pdf)
        ok = 0
        for v in vs:
            # walidacja: liczba radnych == agg za+przeciw+wstrzym (+nieobecni/brak)
            agg = v["agg"]
            total = (agg["za"] or 0) + (agg["przeciw"] or 0) + (agg["wstrzym"] or 0) + \
                    len(v["named"]["nieobecni"]) + len(v["named"]["brak_glosu"])
            if v["pairs_n"] and v["pairs_n"] == total:
                ok += 1
            rec = dict(v)
            rec["session_date"] = s["date"]
            rec["session_num"] = s["roman"]
            records.append(rec)
        print(f"  {s['roman']:5s} {s['date']} votes={len(vs)} validated={ok}")
    return records


# ---- 4. Budowa wyjścia (struktura jak zory/siemianowice) ----
NAME_AGG = {}
_all_session_dates = []


def _compute_consensus(all_votes):
    club_majority = {}
    for v in all_votes:
        by_club = defaultdict(list)
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                by_club[club_of(name)].append(cat)
        for cl, cats in by_club.items():
            if cats:
                club_majority[(cl, v["id"])] = Counter(cats).most_common(1)[0][0]
    stats = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0, "brak": 0,
                                 "nieobecny": 0, "with": 0, "against": 0, "sess": set()})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal" if cat == "wstrzymal_sie" \
                    else "nieobecny" if cat == "nieobecni" else "brak"
                stats[name][key] += 1
                if key != "nieobecny":
                    stats[name]["sess"].add(v["session_date"])
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                maj = club_majority.get((club_of(name), v["id"]))
                if maj is None:
                    continue
                if cat == maj:
                    stats[name]["with"] += 1
                else:
                    stats[name]["against"] += 1
    return stats


def build_output(records):
    all_votes, vid = [], 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
            "topic": rec.get("title") or "", "named_votes": named,
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

    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {
            "name": name, "club": club_of(name), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
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
                elif cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    stats = _compute_consensus(all_votes)

    councilors_list = []
    for name in sorted(councilors_data.keys()):
        c = councilors_data[name]
        st = stats[name]
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(st["sess"]) / total_sessions * 100) if total_sessions else 0
        total_decis = st["with"] + st["against"]
        zgodnosc = (st["with"] / total_decis * 100) if total_decis else 0.0
        councilors_list.append({
            "name": name, "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": round(zgodnosc, 1),
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": st["against"], "rebellions": [],
            "has_activity_data": False, "activity": None,
        })

    global NAME_AGG, _all_session_dates
    NAME_AGG = {name: dict(stats[name], sess=len(stats[name]["sess"])) for name in stats}
    _all_session_dates = [s["date"] for s in sessions_data]

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
        score = round(same / len(common) * 100, 1)
        pairs.append({"a": a, "b": b, "club_a": club_of(a), "club_b": club_of(b),
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(club_of(n) for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "sess": set()})
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal_sie" if cat == "wstrzymal_sie" \
                    else "nieobecny" if cat == "nieobecni" else "brak"
                cv[name][key] += 1
                if key != "nieobecny":
                    cv[name]["sess"].add(d)
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        agg = NAME_AGG.get(name, {})
        all_sess = len(vd["sess"])
        frekw = 100.0 * all_sess / len(_all_session_dates) if _all_session_dates else 0.0
        dec = agg.get("with", 0) + agg.get("against", 0)
        zgod = 100.0 * agg.get("with", 0) / dec if dec else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {
                KADENCJA_ID: {
                    "club": club_of(name), "has_voting_data": True,
                    "has_activity_data": False, "frekwencja": round(frekw, 1),
                    "aktywnosc": round(float(vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) /
                                       total * 100, 1),
                    "zgodnosc_z_klubem": round(zgod, 1),
                    "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                    "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                    "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                    "rebellion_count": agg.get("against", 0), "rebellions": [],
                    "roles": [], "notes": "", "former": False, "mid_term": False,
                }
            }
        })
    return {"profiles": profiles}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("=== Scraper Rada Miejska w Mogilnie (Nefeni bip.mogilno.pl) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kadencji z glosowaniem-imienne: {len(sessions)}")
    if not sessions:
        print("  BRAK SESJI.")
        sys.exit(1)
    records = collect_all(sessions, cache_dir)
    print(f"  Razem głosowań: {len(records)}")
    if not records:
        print("  BRAK DANYCH — nic do zapisania.")
        sys.exit(1)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    total = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Sesji: {total['total_sessions']}, głosowań: {total['total_votes']}, "
          f"radnych: {total['total_councilors']}")


if __name__ == "__main__":
    main()
