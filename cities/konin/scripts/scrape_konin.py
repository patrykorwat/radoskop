#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Konin — imienne głosowania Rady Miasta Konina.

Źródło: BIP UM Konin (bipum.konin.eu, własny CMS "Rekord").
Rada Miasta Konina (IX kadencja 2024-2029) publikuje w kategorii
"Głosowania z sesji Rady Miasta Konina" (/6499) per-sesyjne dokumenty z
wynikami głosowań imiennych (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIE GŁOSOWAŁ /
NIEOBECNY per radny, temat głosowania, sesja i godzina).

DWA formaty PDF w sesjach IX kadencji:
  * nowy (od ok. XXXV/2025-05): jeden zbiorczy PDF per sesja, bloki
    "Głosowanie w sprawie: <temat>." + tabela "Lp. Imię i nazwisko Głos
    Data i czas oddania głosu" (ZA / Przeciw / Wstrzymał się / Nie głosował /
    Nieobecny).
  * stary (2024-05..~2025-04): jeden PDF per druk (głosowanie), nagłówek
    "N Sesja Rady Miasta Konina / Głosowanie / <temat> (druk nr X)." +
    dwukolumnowa tabela "Lp. Nazwisko i imię Głos" (ZA / PRZECIW /
    WSTRZYMUJĘ SIĘ / NIEOBECNY / NIE GŁOSOWAŁ).

Skład rady zmieniał się w trakcie kadencji (Witold Nowak i Monika Piguła
zastąpieni m.in. przez Jarosława Derdzińskiego i Marię Królikowską) — lista
radnych = suma nazwisk we wszystkich głosowaniach IX kadencji.

Kluby radnych: kuratorowane z BIP (kategoria "Kluby Radnych" /6366 — oświadczenia
o powołaniu klubów: Wspólny Konin, Prawo i Sprawiedliwość, Koalicja Obywatelska,
Obywatele Konina; Konińska Prawica Wschodniej Wielkopolski rozwiązana 2024-12-02).
Członkowie nieprzypisani do żadnego czynnego klubu -> NZ.

Użycie:
    python scrape_konin.py --output docs/data.json --profiles docs/profiles.json
                          [--cache-dir .cache]
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

BIP = "https://bipum.konin.eu"
VOTES_CAT = "6499"          # "Głosowania z sesji Rady Miasta Konina"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PAGE_CAP = 12

REQ_DELAY = 1.1
_LAST_REQ = 0.0


# ---- Kluby radnych (kuratorowane z BIP /6366) ----
CLUBS_META = {
    "WK": {"name": "Wspólny Konin", "color": "#0ea5e9",
           "bg": "rgba(14,165,233,0.12)", "avatar_bg": "#0369a1"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "KO": {"name": "Koalicja Obywatelska", "color": "#f59e0b",
           "bg": "rgba(245,158,11,0.12)", "avatar_bg": "#b45309"},
    "OB": {"name": "Obywatele Konina", "color": "#16a34a",
           "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
    "NZ": {"name": "Niezrzeszeni", "color": "#6b7280",
           "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}

# Przynależność klubowa z oświadczeń o powołaniu klubów (BIP /6366, 2024-05/06)
# + rezygnacja/skład członkowski; KPWW rozwiązana 2024-12-02 -> członkowie NZ.
CLUB_ASSIGN = {
    # Koalicja Obywatelska (2024-05-06)
    "Urszula Maciaszek": "KO", "Monika Lis": "KO", "Katarzyna Wagner": "KO",
    "Joachim Sikorski": "KO", "Wiesław Steinke": "KO",
    # Prawo i Sprawiedliwość (2024-05-22)
    "Krystian Majewski": "PiS", "Hubert Szczepański": "PiS", "Zenon Chojnacki": "PiS",
    "Krystyna Leśniewska": "PiS", "Katarzyna Jaworska": "PiS",
    # Wspólny Konin (2024-05-06; przew. 2025-10 M. Marcinkowski)
    "Monika Kosińska": "WK", "Małgorzata Krawczyńska": "WK",
    "Mikołaj Marcinkowski": "WK", "Emilia Wasielewska": "WK",
    # Obywatele Konina (2024-06)
    "Tomasz Andrzej Nowak": "OB", "Jarosław Derdziński": "OB",
    "Jarosław Lewandowski": "OB",
    # Pozostali — niezrzeszeni (w tym członkowie rozwiązanej KPWW)
    "Robert Popkowski": "NZ", "Zofia Itman": "NZ", "Sławomir Lachowicz": "NZ",
    "Piotr Czerniejewski": "NZ", "Maria Królikowska": "NZ", "Jarosław Sidor": "NZ",
    # Radni, którzy odeszli w trakcie kadencji
    "Witold Nowak": "NZ", "Monika Piguła": "NZ",
}

# ---- Normalizacja nazwisk (dopasowanie wariantów z PDF do kanonu) ----
_CANON = {
    "Maria Sława Królikowska": "Maria Królikowska",
    "Maria Slawa Krolikowska": "Maria Królikowska",
}


def _norm(s):
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def make_slug(name):
    repl = {'\u0105': 'a', '\u0107': 'c', '\u0119': 'e', '\u0142': 'l', '\u0144': 'n',
            '\u00f3': 'o', '\u015b': 's', '\u017a': 'z', '\u017c': 'z',
            '\u0104': 'A', '\u0106': 'C', '\u0118': 'E', '\u0141': 'L', '\u0143': 'N',
            '\u00d3': 'O', '\u015a': 'S', '\u0179': 'Z', '\u017b': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name):
    return CLUB_ASSIGN.get(name, "NZ")


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
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"},
                        timeout=60, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data, encoding="utf-8", errors="ignore")
    return data


_MONTHS = {"01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6,
           "07": 7, "08": 8, "09": 9, "10": 10, "11": 11, "12": 12}


def _roman_to_int(roman):
    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    n = 0
    prev = 0
    for ch in reversed(roman.upper()):
        v = vals.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji z kategorii /6499
# ---------------------------------------------------------------------------
def collect_sessions(cache_dir=None):
    """Zwraca [{docid, date, roman, num}] — sesje IX kadencji (date >= 2024-05-07)."""
    out = []
    seen = set()
    for page in range(1, PAGE_CAP + 1):
        url = f"{BIP}/{VOTES_CAT}/strona/{page}"
        try:
            html = fetch(url, cache_dir)
        except Exception as e:
            print(f"    [warn] page {page}: {e}")
            break
        rows = re.findall(r'href="6499/dokument/(\d+)"[^>]*>(.*?)</a>', html, re.S)
        new = 0
        for docid, anchor in rows:
            if docid in seen:
                continue
            seen.add(docid)
            a = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", anchor)).strip()
            m = re.search(r"Głosowania z ([\w]+) sesji RMK - (\d{1,2})\.(\d{1,2})\.(\d{4})", a)
            if not m:
                continue
            roman, dd, mm, yyyy = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
            date = f"{yyyy:04d}-{mm}-{dd:02d}"
            if date < KAD_START:
                continue
            out.append({"docid": docid, "date": date, "roman": roman,
                        "num": _roman_to_int(roman)})
            new += 1
        print(f"    page {page}: sessions total {len(out)}")
        if new == 0:
            break
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF głosowań (dwa formaty)
# ---------------------------------------------------------------------------
_FORMAT_A_VOTE = re.compile(
    r"(Za|Przeciw|Wstrzymał się|Wstrzymała się|Nie głosował|Nie głosowała|"
    r"Nieobecny|Nieobecna|Obecny|Obecna)", re.IGNORECASE)
_FORMAT_B_VOTE = re.compile(
    r"(NIEOBECN[AIY]|NIE GŁOSOWA[ŁL][AE]?|OBECN[AIY]|"
    r"WSTRZYMUJĘ SIĘ|WSTRZYMAŁ SIĘ|WSTRZYMAŁA SIĘ|PRZECIW|ZA)")


def _lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [l.strip() for p in pdf.pages
                for l in (p.extract_text() or "").split("\n") if l.strip()]


def _clean_vote(vote):
    v = vote.strip().lower()
    v = v.replace("\u0142", "l").replace("\u0119", "e")
    if v in ("za",):
        return "za"
    if "przeciw" in v:
        return "przeciw"
    if "wstrzym" in v:
        return "wstrzymal_sie"
    if "nie glosowal" in v or "nie głosował" in v:
        return "brak_glosu"
    if "nieobecn" in v:
        return "nieobecni"
    if "obecn" in v:  # OBECNY/OBECNA — obecny, nie oddał głosu
        return "brak_glosu"
    return None


def _parse_format_b(data, session_date):
    """Stary format: jeden PDF per głosowanie."""
    lines = _lines(data)
    # temat: między 'Głosowanie' a '(druk nr ...)'
    topic_lines = []
    in_topic = False
    for l in lines:
        if re.fullmatch(r"Głosowanie", l):
            in_topic = True
            continue
        if in_topic:
            if "(druk nr" in l or "druk nr" in l:
                break
            topic_lines.append(l)
    topic = re.sub(r"\s+", " ", " ".join(topic_lines)).strip().rstrip(".").strip()
    # dane szczegółowe: dwukolumnowa tabela 'Lp Nazwisko i imię Głos'
    names = []
    in_tab = False
    for l in lines:
        if "Uprawnieni do głosowania" in l:
            in_tab = True
            continue
        if in_tab:
            if "Wydrukowano" in l:
                break
            last = 0
            for m in _FORMAT_B_VOTE.finditer(l):
                cell = l[last:m.start()]
                hm = re.match(r"^\s*(\d+)\.?\s+(.+?)\s*$", cell)
                if hm:
                    nm = hm.group(2).strip().rstrip(".").strip()
                    v = _clean_vote(m.group(1))
                    if nm and v:
                        names.append((nm, v))
                last = m.end()
    if not names:
        return None
    counts = Counter(c for _, c in names)
    return {"session_date": session_date, "topic": topic, "named": {
        "za": [n for n, c in names if c == "za"],
        "przeciw": [n for n, c in names if c == "przeciw"],
        "wstrzymal_sie": [n for n, c in names if c == "wstrzymal_sie"],
        "brak_glosu": [n for n, c in names if c == "brak_glosu"],
        "nieobecni": [n for n, c in names if c == "nieobecni"],
    }, "counts": dict(counts)}


def _parse_format_a_block(lines, start, end, session_date):
    first = lines[start]
    m = re.match(r"^Głosowanie w sprawie:\s*(.*)$", first)
    if not m:
        return None
    topic = m.group(1).strip().rstrip(".").strip()
    # tabela szczegółowa
    names = []
    for l in lines[start:end]:
        rm = re.match(
            r"^(\d+)\s+(.+?)\s+(Za|Przeciw|Wstrzymał się|Wstrzymała się|"
            r"Nie głosował|Nie głosowała|Nieobecny|Nieobecna)\s+"
            r"([\d\.]+ [\d:]+|-+)$", l)
        if rm:
            name = rm.group(2).strip()
            v = _clean_vote(rm.group(3))
            if v:
                names.append((name, v))
    if not names:
        return None
    return {"session_date": session_date, "topic": topic, "named": {
        "za": [n for n, c in names if c == "za"],
        "przeciw": [n for n, c in names if c == "przeciw"],
        "wstrzymal_sie": [n for n, c in names if c == "wstrzymal_sie"],
        "brak_glosu": [n for n, c in names if c == "brak_glosu"],
        "nieobecni": [n for n, c in names if c == "nieobecni"],
    }, "counts": {}}


def _parse_format_a(data, session_date):
    lines = _lines(data)
    block_starts = [i for i, l in enumerate(lines)
                    if re.match(r"^Głosowanie w sprawie:", l)]
    votes = []
    for bi, start in enumerate(block_starts):
        end = block_starts[bi + 1] if bi + 1 < len(block_starts) else len(lines)
        v = _parse_format_a_block(lines, start, end, session_date)
        if v:
            votes.append(v)
    return votes


def parse_pdf(data, session_date):
    """Wykrywa format i zwraca listę głosowań dla jednego PDF."""
    txt = None
    try:
        txt = "\n".join(_lines(data))
    except Exception:
        return []
    if "Głosowanie w sprawie:" in txt:
        return _parse_format_a(data, session_date)
    if "Uprawnieni do głosowania" in txt or "Wydrukowano" in txt:
        v = _parse_format_b(data, session_date)
        return [v] if v else []
    return []


# ---------------------------------------------------------------------------
# 3. Główna kolekcja: sesje -> dokumenty -> PDF-y -> głosowania
# ---------------------------------------------------------------------------
def _canonical(name):
    if name in _CANON:
        return _CANON[name]
    return name


def collect_all(sessions, cache_dir=None):
    records = []
    for s in sessions:
        docurl = f"{BIP}/{VOTES_CAT}/dokument/{s['docid']}"
        try:
            html = fetch(docurl, cache_dir)
        except Exception as e:
            print(f"  [warn] doc {s['docid']}: {e}")
            continue
        atts = re.findall(r"api/download/file\?id=(\d+)", html)
        atts = list(dict.fromkeys(atts))
        if not atts:
            print(f"  [warn] {s['roman']} {s['date']}: brak załączników")
            continue
        nvotes = 0
        for aid in atts:
            pdfurl = f"{BIP}/api/download/file?id={aid}"
            try:
                data = fetch(pdfurl, cache_dir, binary=True)
            except Exception as e:
                print(f"    [warn] pdf {aid}: {e}")
                continue
            vs = parse_pdf(data, s["date"])
            for v in vs:
                rec = dict(v)
                rec["session_num"] = s["roman"]
                rec["cluster_key"] = s["date"]
                records.append(rec)
                nvotes += 1
        print(f"  {s['roman']:8s} {s['date']} votes={nvotes} (from {len(atts)} pdf)")
    return records


# ---------------------------------------------------------------------------
# 4. Budowa wyjścia
# ---------------------------------------------------------------------------
def _canonical_rec(rec):
    for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
        rec["named"][k] = [_canonical(n) for n in rec["named"].get(k, [])]
    return rec


def _compute_consensus(all_votes):
    """Spójne statystyki per radny + zgodność z klubem (większość klubu).

    Zwraca (club_majority, name_stats): club_majority[(club, vote_id)] -> 'za',...
    name_stats[name] -> {za,przeciw,wstrzymal,brak,nieobecny, present(sesje jako
    głosujący), with, against}."""
    club_majority = {}
    for v in all_votes:
        by_club = defaultdict(list)
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                by_club[_club_of(name)].append(cat)
        for cl, cats in by_club.items():
            if cats:
                club_majority[(cl, v["id"])] = Counter(cats).most_common(1)[0][0]
    stats = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0, "brak": 0,
                                 "nieobecny": 0, "with": 0, "against": 0,
                                 "sess": set()})
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
                maj = club_majority.get((_club_of(name), v["id"]))
                if maj is None:
                    continue
                if cat == maj:
                    stats[name]["with"] += 1
                else:
                    stats[name]["against"] += 1
    return club_majority, stats


def build_output(records):
    all_votes = []
    vid = 0
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
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d,
            "session_number": rec.get("session_num", ""),
            "topic": rec["topic"] or "", "named_votes": named,
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
            "name": name, "club": _club_of(name), "district": None,
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

    # per-radna aggr dla profili (wspólny z build_profiles)
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
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
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
    # liczniki głosów per radny (bez zaangażowania klubowej większości w profilu)
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
                    "club": _club_of(name), "has_voting_data": True,
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

    print("=== Scraper Rada Miasta Konin (bipum.konin.eu /6499) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kadencji: {len(sessions)}")

    records = collect_all(sessions, cache_dir)
    print(f"  Razem glosowan: {len(records)}")

    for r in records:
        _canonical_rec(r)

    if not records:
        print("  BRAK DANYCH — nic do zapisania.")
        sys.exit(1)

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    total = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Sesji: {total['total_sessions']}, glosowan: {total['total_votes']}, "
          f"radnych: {total['total_councilors']}")


if __name__ == "__main__":
    main()
