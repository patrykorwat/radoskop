#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Żory — imienne głosowania Rady Miasta Żory (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miasta Żory (platforma Nefeni „Nowoczesna Gmina”,
https://bip.zory.pl, API https://bip-api.zory.pl). Rada Miasta publikuje w
kategorii „Rada Miasta → Kadencja Rady Miasta 2024-2029 → Sesje Rady Miasta →
Protokoły” (podkategorie per rok: rok 2024 / 2025 / 2026) per-sesyjne protokoły,
a każdy artykuł protokołu ma załącznik „raport z głosowań.pdf” z wynikami
głosowań imiennych (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI per
radny, temat głosowania, data sesji) — format tekstowy eSesja.

Format raportu (24 sesje protokołów):
  Rada Miasta Żory
  Radni
  Raport z głosowań
  XXVII Sesja w dniu 25 czerwca 2026
  Przeprowadzone głosowania
  1. Głosowanie w sprawie {temat} - czas głosowania: ..., wyniki: ZA: X, PRZECIW: Y, ...
  Wyniki imienne: Jacek ARASIM (ZA), Kazimierz DAJKA (WSTRZYMUJĘ SIĘ), ...

Głosowanie „Sprawdzenie obecności” (wyniki OBECNY/NIEOBECNY) jest pomijane.

Kluby radnych: kuratorowane z BIP (kategoria „Kluby Radnych”, stan IX kadencji):
  * Koalicja Obywatelska (KO): Kamil Owczarek (przew.), Anna Gaszka, Ewa Kałus,
    Mateusz Mleczko, Anna Nowacka, Weronika Porada, Anna Ujma
  * Prawo i Sprawiedliwość (PiS): Krzysztof Mentlik (przew.), Małgorzata Celińska,
    Dariusz Domański, Mieczysław Jakubowski, Grzegorz Książek, Krzysztof Kurek,
    Jacek Świerkocki
  * Żorskie Porozumienie i Waldemar Socha (ZP): Michał Miłek (przew.), Jacek Arasim,
    Barbara Fiedor, Jolanta Hrycak, Piotr Kosztyła, Wojciech Maroszek
  * Żorska Samorządność (ZS): Kazimierz Dajka (przew.), Henryk Oszek, Dawid Świerczek
  * Byli radni tracący mandat w trakcie kadencji (Daniel Wawrzyczek, Mateusz Buksa,
    Piotr Huzarewicz) -> NZ (niezrzeszeni/poza aktualnym klubem).

Użycie:
    python scrape_zory.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
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

API = "https://bip-api.zory.pl"
SITE = "https://bip.zory.pl"
# Kategorie protokołów per rok (IX kadencja) — kategorie Rady Miasta
YEAR_CATS = [
    "kategorie/2669-rok-2024",
    "kategorie/2995-rok-2025",
    "kategorie/3311-rok-2026",
]
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.6
_LAST_REQ = 0.0
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"}

# ---- Kluby radnych (kuratorowane z BIP /kategorie/3078-kluby-radnych-, IX kad.) ----
CLUBS_META = {
    "KO": {"name": "Koalicja Obywatelska", "color": "#f59e0b",
           "bg": "rgba(245,158,11,0.12)", "avatar_bg": "#b45309"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "ZP": {"name": "Żorskie Porozumienie i Waldemar Socha", "color": "#16a34a",
           "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
    "ZS": {"name": "Żorska Samorządność", "color": "#a855f7",
           "bg": "rgba(168,85,247,0.12)", "avatar_bg": "#7e22ce"},
    "NZ": {"name": "Niezrzeszeni", "color": "#6b7280",
           "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}

CLUB_ASSIGN = {
    # Koalicja Obywatelska
    "Kamil Owczarek": "KO", "Anna Gaszka": "KO", "Ewa Kałus": "KO",
    "Mateusz Mleczko": "KO", "Anna Nowacka": "KO", "Weronika Porada": "KO",
    "Anna Ujma": "KO",
    # Prawo i Sprawiedliwość
    "Krzysztof Mentlik": "PiS", "Małgorzata Celińska": "PiS",
    "Dariusz Domański": "PiS", "Mieczysław Jakubowski": "PiS",
    "Grzegorz Książek": "PiS", "Krzysztof Kurek": "PiS", "Jacek Świerkocki": "PiS",
    # Żorskie Porozumienie i Waldemar Socha
    "Michał Miłek": "ZP", "Jacek Arasim": "ZP", "Barbara Fiedor": "ZP",
    "Jolanta Hrycak": "ZP", "Piotr Kosztyła": "ZP", "Wojciech Maroszek": "ZP",
    # Żorska Samorządność
    "Kazimierz Dajka": "ZS", "Henryk Oszek": "ZS", "Dawid Świerczek": "ZS",
}


def _norm(s):
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def club_of(name):
    return CLUB_ASSIGN.get(name, "NZ")


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
            'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
            'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def canonical_name(raw):
    """'Jacek ARASIM' / 'Jolanta Hrycak' -> 'Jacek Arasim' (title case, kanon)."""
    raw = raw.strip()
    parts = raw.split()
    return " ".join(p[:1].upper() + p[1:].lower() if p else "" for p in parts)


# ---- HTTP z cache ----
def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url, cache_dir=None, binary=False):
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".json"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=UA, timeout=120)
    resp.raise_for_status()
    if binary:
        data = resp.content
        if cache_dir is not None:
            (cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".bin")).write_bytes(data)
        return data
    data = resp.text
    if cache_dir is not None:
        (cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".json")).write_text(data, encoding="utf-8")
    return json.loads(data)


# ---- 1. Kolekcja artykułów protokołów (kategorie per rok) ----
_ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}


def _roman_to_int(roman):
    n, prev = 0, 0
    for ch in reversed(roman.upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n if n else 0


_TITLE_RE = re.compile(
    r"Nr\s+([IVXLCDM]+)/\d+\s+z\s+sesji.*?z\s+dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")


def parse_title(title):
    """Zwraca (roman, iso_date) z tytułu 'Protokół Nr XXVII/26 ... z dnia 25.06.2026r.'."""
    m = _TITLE_RE.search(title)
    if not m:
        return "", ""
    roman = m.group(1)
    dd, mm, yyyy = m.group(2), m.group(3), m.group(4)
    return roman, f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def collect_sessions(cache_dir=None):
    """Zwraca [{article_id, article_slug, title, roman, date, num}] — IX kadencja."""
    out, seen = [], set()
    for cat in YEAR_CATS:
        data = fetch(f"{API}/api/page-content/{cat}?lang=PL", cache_dir)
        item = (data.get("contentData") or {}).get("item") or {}
        for a in item.get("articles") or []:
            slug = a.get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            title = a.get("title") or ""
            roman, date = parse_title(title)
            if not date or date < KAD_START:
                continue
            out.append({
                "article_slug": slug, "title": title, "roman": roman,
                "date": date, "num": _roman_to_int(roman),
            })
    # sort chronologicznie
    out.sort(key=lambda s: s["date"])
    return out


# ---- 2. Załącznik raportu + parsowanie PDF ----
def raport_url_for_article(article_slug, cache_dir=None):
    data = fetch(f"{API}/api/page-content/{article_slug}?lang=PL", cache_dir)
    item = (data.get("contentData") or {}).get("item") or {}
    for at in item.get("attachments") or []:
        dt = (at.get("displayText") or "").lower()
        if "raport" in dt or "głosow" in dt or "glosow" in dt:
            return at.get("url")
    # fallback: pierwszy plik PDF
    for at in item.get("attachments") or []:
        if at.get("url"):
            return at.get("url")
    return None


def _lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [l.strip() for p in pdf.pages
                for l in (p.extract_text() or "").split("\n") if l.strip()]


_BLOCK_RE = re.compile(r'^(\d+)\.\s+Głosowanie w sprawie')
_ENTRY_RE = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$')
_CATS = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")


def parse_pdf(data):
    try:
        ls = _lines(data)
    except Exception:
        return []
    starts = [i for i, l in enumerate(ls) if _BLOCK_RE.match(l)]
    if not starts:
        return []
    votes = []
    for bi, st in enumerate(starts):
        en = starts[bi + 1] if bi + 1 < len(starts) else len(ls)
        joined = " ".join(ls[st:en])
        marker = joined.find("Wyniki imienne:")
        if marker < 0:
            continue
        first = _BLOCK_RE.sub("", ls[st]).strip()
        # temat: usuń ' - czas głosowania...' i dalszy ciąg
        topic = re.split(r"[-–]\s+czas\s+głosowania", first)[0].strip()
        im = joined[marker + len("Wyniki imienne:"):]
        named = {c: [] for c in _CATS}
        for entry in im.split(","):
            entry = entry.strip()
            if not entry:
                continue
            m = _ENTRY_RE.match(entry)
            if not m:
                continue
            name, vote = m.group(1).strip(), m.group(2).strip().upper()
            if name == "Przygotował":
                continue
            name = canonical_name(name)
            if vote == "ZA":
                named["za"].append(name)
            elif vote == "PRZECIW":
                named["przeciw"].append(name)
            elif "WSTRZYM" in vote:
                named["wstrzymal_sie"].append(name)
            elif "BRAK" in vote:
                named["brak_glosu"].append(name)
            elif "NIEOBEC" in vote:
                named["nieobecni"].append(name)
            # OBECNY -> attendance, pomiń
        if not (named["za"] or named["przeciw"] or named["wstrzymal_sie"]):
            continue
        votes.append({"topic": topic, "named": named})
    return votes


# ---- 3. Kolekcja wszystkich głosowań ----
def collect_all(sessions, cache_dir=None):
    records = []
    for s in sessions:
        try:
            ru = raport_url_for_article(s["article_slug"], cache_dir)
            if not ru:
                print(f"  [warn] {s['roman']} {s['date']}: brak załącznika raportu")
                continue
            pdf = fetch(ru, cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['roman']} {s['date']}: {e}")
            continue
        vs = parse_pdf(pdf)
        for v in vs:
            rec = dict(v)
            rec["session_date"] = s["date"]
            rec["session_num"] = s["roman"]
            records.append(rec)
        print(f"  {s['roman']:6s} {s['date']} votes={len(vs)}")
    return records


# ---- 4. Budowa wyjścia (struktura jak siemianowice/krosno) ----
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
    return club_majority, stats


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
        for cat in _CATS:
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d,
            "session_number": rec.get("session_num", ""),
            "topic": rec.get("topic") or "", "named_votes": named,
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
    _, stats = _compute_consensus(all_votes)

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

    global NAME_AGG
    global _all_session_dates
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
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {
        "generated": datetime.now().isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [kad],
    }


NAME_AGG = {}
_all_session_dates = []


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

    print("=== Scraper Rada Miasta Żory (Nefeni /kategorie/2668-protokoly) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Protokołów/sesji IX kadencji: {len(sessions)}")
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
