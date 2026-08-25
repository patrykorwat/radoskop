#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Siemianowice Śląskie — imienne głosowania Rady Miasta.

Źródło: BIP Urzędu Miasta Siemianowice Śląskie (platforma Finn.pl,
bip.msiemianowicesl.finn.pl). Rada Miasta (IX kadencja 2024-2029) publikuje
w kategorii "głosowania na sesjach rady miasta → głosowania 9. kadencja"
(/bipkod/35184442) per-sesyjne PDF-y "Wykaz głosowań z NN Sesji ..." z wynikami
głosowań imiennych (ZA / PRZECIW / WSTRZYMAŁ SIĘ / NIEOBECNY per radny, temat
głosowania, data sesji).

DWA formaty PDF (obuźródłowe z systemu eSesja):
  * Format A (od ~III/IV 2024): nagłówek "NN Sesja ... z dnia ...", bloki
    "2.1. Głosowanie w sprawie ..." + "podsumowanie" + tabela "Wyniki imienne"
    (lp / nazwisko / imię / głos; głos = ZA|PRZECIW|WSTRZYMAŁ SIĘ|WSTRZYMAŁA
    SIĘ|nieobecny).
  * Format B (I i II sesja, 05-06.05.2024): nagłówek "I SESJA ... – 6.05.2024 r.",
    bloki "Wynik głosowania nr N. Punkt nr M. <temat>" + lista "Radni: ... - ZA"
    (głos = ZA|PRZECIW|NIEOBECNA|...).

Kluby radnych: kuratorowane z BIP (kategoria "kluby radnych 9. kadencji
2024-2029" /bipkod/35499054, dokument "KLUBY zmiana od 18.06.2026 r."):
  * SMS (Siemianowicki Ruch Miejski): Tomasz Nowara (przew.), Grzegorz Mól,
    Damian Achtelik, Michał Blacha
  * Prawo i Sprawiedliwość: Danuta Sobczyk (przew.), Barbara Patyk-Płuciennik,
    Klaudiusz Michna
  * Twoi Samorządowcy: Renata Jaroń-Guzy (przew.), Alicja Piech, Jakub Piech,
    Marta Suchanek-Bijak
  * Koalicja Obywatelska: Anna Zasada-Chorab (przew.), Beata Breguła, Beata
    Ziemianek, Wojciech Okoń, Marcin Janota, Anna Żejmo, Patrycja Woźniczko,
    Bartosz Dudzik
  * Samorząd Jedności: Paweł Siegel (przew.), Adam Cebula, Adam Klacka
  * Łukasz Rosicki resignował (rezygnacja z PiS) -> NZ (były radny)

Użycie:
    python scrape_siemianowice_slaskie.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
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

BIP = "https://bip.msiemianowicesl.finn.pl"
VOTES_CAT = "bipkod/35184442"   # głosowania 9. kadencji 2024-2029
KAD_START = "2024-04-08"  # niższy próg: sesja I (06.05.2024) też jest IX kadencją
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PAGE_CAP = 10

REQ_DELAY = 1.0
_LAST_REQ = 0.0

# ---- Kluby radnych (kuratorowane z BIP /bipkod/35499054, stan 18.06.2026) ----
CLUBS_META = {
    "SMS": {"name": "SMS (Siemianowicki Ruch Miejski)", "color": "#0ea5e9",
            "bg": "rgba(14,165,233,0.12)", "avatar_bg": "#0369a1"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "TS": {"name": "Twoi Samorządowcy", "color": "#16a34a",
           "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
    "KO": {"name": "Koalicja Obywatelska", "color": "#f59e0b",
           "bg": "rgba(245,158,11,0.12)", "avatar_bg": "#b45309"},
    "SJ": {"name": "Samorząd Jedności", "color": "#a855f7",
           "bg": "rgba(168,85,247,0.12)", "avatar_bg": "#7e22ce"},
    "NZ": {"name": "Niezrzeszeni", "color": "#6b7280",
           "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}

# Przynależność klubowa (stan 18.06.2026, kuratorowane z BIP).
# Klucze = kanoniczny zapis z danych "Imię Nazwisko" (zgodny z normalizacją).
CLUB_ASSIGN = {
    # SMS
    "Tomasz Nowara": "SMS", "Grzegorz Mól": "SMS", "Damian Achtelik": "SMS",
    "Michał Blacha": "SMS",
    # Prawo i Sprawiedliwość
    "Danuta Sobczyk": "PiS", "Barbara Patyk-Płuciennik": "PiS",
    "Klaudiusz Michna": "PiS",
    # Twoi Samorządowcy
    "Renata Jaroń-Guzy": "TS", "Alicja Piech": "TS", "Jakub Piech": "TS",
    "Marta Suchanek-Bijak": "TS",
    # Koalicja Obywatelska
    "Anna Zasada-Chorab": "KO", "Beata Breguła": "KO", "Beata Ziemianek": "KO",
    "Wojciech Okoń": "KO", "Marcin Janota": "KO", "Anna Żejmo": "KO",
    "Patrycja Woźniczko": "KO", "Bartosz Dudzik": "KO",
    # Samorząd Jedności
    "Paweł Siegel": "SJ", "Adam Cebula": "SJ", "Adam Klacka": "SJ",
    # Rezygnacja w trakcie kadencji (Rosicki odszedł z PiS -> NZ)
    "Łukasz Rosicki": "NZ",
}

# Normalizacja nazwisk (dopasowanie wariantów z PDF do kanonu).
_CANON_SPACE_HYPHEN = re.compile(r"(\S)- (?=\S)")   # "Jaroń- Guzy" -> "Jaroń-Guzy"
_CANON = {
    # XIX Uroczysta (2025-06) — litera-po-literze spacje kolumnowe w tabeli imiennej
    "C e b ula Adam": "Adam Cebula", "N owara T o m asz": "Tomasz Nowara",
    "P iech Alicja": "Alicja Piech", "S u chanek-Bijak Marta": "Marta Suchanek-Bijak",
}


def _norm(s):
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
            'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
            'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name):
    return CLUB_ASSIGN.get(name, "NZ")


def _canonical_name(raw):
    """Normalizuje nazwisko z PDF do kanonu 'Imię Nazwisko'."""
    raw = raw.strip().rstrip(".").strip()
    # uporządkuj złożone nazwiska ("Jaroń- Guzy Renata" -> "Jaroń-Guzy Renata")
    raw = _CANON_SPACE_HYPHEN.sub(lambda m: m.group(1) + "-", raw)
    if raw in _CANON:
        return _CANON[raw]
    # PDF-y dają "Nazwisko Imię" -> zamień na "Imię Nazwisko"
    parts = raw.split()
    if len(parts) >= 2:
        return f"{parts[-1]} {' '.join(parts[:-1])}"
    return raw


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
                        timeout=90, verify=False)
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


_MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}

_ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}


def _roman_to_int(roman):
    n = 0
    prev = 0
    for ch in reversed(roman.upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n if n else 0


def _parse_roman_from_title(title):
    m = re.search(r'([IVXLCDM]{1,7})(?:/?\d{4})?\s*(?:z\s+)?(?:Uroczystej\s+)?Sesj', title)
    if m:
        return m.group(1)
    # fallback: pierwszy rzymski w tytule
    m = re.search(r'\b([IVXLCDM]{1,7})\b', title)
    return m.group(1) if m else ""


def _parse_date_from_title(title):
    """Zwraca 'YYYY-MM-DD' z DD.MM.YYYY lub 'z DD miesiąc YYYY' z tytułu."""
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', title)
    if m:
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    m = re.search(r'(?:z dnia|z|odbytej w dniu|w dniu|z\s+)\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', title)
    if m:
        dd, mon, yyyy = m.group(1), m.group(2).lower(), m.group(3)
        mm = _MONTHS_PL.get(mon)
        if mm:
            return f"{yyyy}-{mm:02d}-{int(dd):02d}"
    return None


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji z kategorii (stronnicowana ?start=N)
# ---------------------------------------------------------------------------
def collect_sessions(cache_dir=None):
    """Zwraca [{fileid, date, roman, num}] — sesje IX kadencji."""
    out = []
    seen = set()
    for page in range(0, PAGE_CAP + 1):
        url = f"{BIP}/{VOTES_CAT}" + (f"?start={page}" if page else "")
        try:
            html = fetch(url, cache_dir)
        except Exception as e:
            print(f"    [warn] page {page}: {e}")
            break
        rows = re.findall(r'href="(/res/serwisy/pliki/(\d+)[^"]*)"[^>]*>(.*?)</a>', html, re.S)
        new = 0
        for full, fileid, anchor in rows:
            if fileid in seen:
                continue
            seen.add(fileid)
            a = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", anchor)).strip()
            date = _parse_date_from_title(a)
            roman = _parse_roman_from_title(a)
            if not date:
                print(f"    [warn] brak daty w tytule: {a[:60]}")
                continue
            if date < KAD_START:
                continue
            out.append({"fileid": fileid, "url": BIP + full, "date": date,
                        "roman": roman, "num": _roman_to_int(roman), "numeral": a[:40]})
            new += 1
        print(f"    page {page}: sessions total {len(out)}")
        if new == 0:
            break
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF głosowań (dwa formaty)
# ---------------------------------------------------------------------------
_VOTE_TOKEN_A = re.compile(
    r"(ZA|PRZECIW|WSTRZYMAŁ SIĘ|WSTRZYMAŁA SIĘ|WSTRZYMUJE SIĘ|"
    r"nie głosował|nie głosowała|nieobecny|nieobecna|nieobecni|"
    r"NIE GŁOSOWAŁ|NIE GŁOSOWAŁA|NIEOBECN[AYI])", re.IGNORECASE | re.UNICODE)
_VOTE_TOKEN_B = re.compile(
    r"(ZA|PRZECIW|WSTRZYMAŁ SIĘ|WSTRZYMAŁA SIĘ|WSTRZYMUJE SIĘ|"
    r"NIE GŁOSOWAŁ|NIE GŁOSOWAŁA|NIEOBECNY|NIEOBECNA)")
_BLOCK_START = re.compile(r"^\d+(?:\.\d+)*\.?\s+Głosowanie", re.IGNORECASE)
# XIX Uroczysta: "Głosowanie w sprawie ..." bez prefiksu liczbowego (capital G)
_BLOCK_START_XIX = re.compile(r"^Głosowanie w sprawie")


def _lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [l.strip() for p in pdf.pages
                for l in (p.extract_text() or "").split("\n") if l.strip()]


def _clean_vote(vote):
    v = vote.strip().lower()
    v = v.replace("\u0142", "l").replace("\u0119", "e")
    if v == "za":
        return "za"
    if "przeciw" in v:
        return "przeciw"
    if "wstrzym" in v:
        return "wstrzymal_sie"
    if "nie glosowal" in v or "nie głosował" in v:
        return "brak_glosu"
    if "nieobecn" in v:
        return "nieobecni"
    return None


def _canonical_name_b(name):
    """Format B: 'Achtelik Damian' / 'Jaroń- Guzy Renata' -> 'Jaroń-Guzy Renata'."""
    name = name.strip().rstrip(".").strip()
    name = _CANON_SPACE_HYPHEN.sub(lambda m: m.group(1) + "-", name)
    return _canonical_name(name)


def _parse_format_b(data, session_date):
    lines = _lines(data)
    topics = []
    for i, l in enumerate(lines):
        if l.startswith("Wynik głosowania nr"):
            topics.append(i)
    votes = []
    for ti, start in enumerate(topics):
        end = topics[ti + 1] if ti + 1 < len(topics) else len(lines)
        block = lines[start:end]
        # temat = pierwsza linia "Wynik głosowania nr N. Punkt nr M. <temat>"
        topic = block[0]
        topic = re.sub(r"^Wynik głosowania nr [\d.]+\.\s*Punkt nr [\d.]+\.?\s*", "", topic).strip()
        names = []
        in_tab = False
        for l in block:
            if l.startswith("Radni:"):
                in_tab = True
                continue
            if in_tab:
                if l.startswith("LICZBA"):
                    break
                m = re.match(r"^(.+?)\s+-\s+(ZA|PRZECIW|WSTRZYMAŁ SIĘ|WSTRZYMAŁA SIĘ|"
                             r"NIE GŁOSOWAŁ|NIE GŁOSOWAŁA|NIEOBECNY|NIEOBECNA)\s*$", l)
                if m:
                    nm = _canonical_name_b(m.group(1))
                    v = _clean_vote(m.group(2))
                    if nm and v:
                        names.append((nm, v))
        if not names:
            continue
        votes.append({"session_date": session_date, "topic": topic, "named": {
            "za": [n for n, c in names if c == "za"],
            "przeciw": [n for n, c in names if c == "przeciw"],
            "wstrzymal_sie": [n for n, c in names if c == "wstrzymal_sie"],
            "brak_glosu": [n for n, c in names if c == "brak_glosu"],
            "nieobecni": [n for n, c in names if c == "nieobecni"],
        }, "counts": {}})
    return votes


_VOTE_FULL = {"za", "przeciw", "wstrzymał się", "wstrzymała się",
              "wstrzymuje się", "nie głosował", "nie głosowała",
              "nieobecny", "nieobecna", "nieobecni"}


def _parse_format_a(data, session_date):
    lines = _lines(data)
    block_starts = []
    for i, l in enumerate(lines):
        if _BLOCK_START.match(l) or _BLOCK_START_XIX.match(l):
            block_starts.append(i)
    votes = []
    for bi, start in enumerate(block_starts):
        end = block_starts[bi + 1] if bi + 1 < len(block_starts) else len(lines)
        block = lines[start:end]
        first = lines[start]
        if _BLOCK_START_XIX.match(first):
            topic = first.strip()
        else:
            topic = _BLOCK_START.sub("", first).strip()
        names = []
        in_im = False
        i = 0
        row_re = re.compile(
            r"^(\d+)\.?\s+([A-Za-zĄ-Żą-żóÓ\- ]+?)(?:\s+(ZA|PRZECIW|WSTRZYMAŁ SIĘ|"
            r"WSTRZYMAŁA SIĘ|WSTRZYMUJE SIĘ|nie głosował|nie głosowała|"
            r"nieobecny|nieobecna|nieobecni))?\s*$")
        while i < len(block):
            l = block[i]
            if l.strip() == "Wyniki imienne":
                in_im = True
                i += 1
                continue
            if not in_im:
                i += 1
                continue
            m = row_re.match(l)
            if m:
                name = m.group(2).strip()
                vote = m.group(3)
                if not vote and i + 1 < len(block) and block[i + 1].strip().lower() in _VOTE_FULL:
                    vote = block[i + 1].strip()
                    i += 2
                else:
                    i += 1
                if name and vote:
                    nm = _canonical_name_b(name)
                    v = _clean_vote(vote)
                    if nm and v:
                        names.append((nm, v))
                continue
            i += 1
        if not names:
            continue
        votes.append({"session_date": session_date, "topic": topic, "named": {
            "za": [n for n, c in names if c == "za"],
            "przeciw": [n for n, c in names if c == "przeciw"],
            "wstrzymal_sie": [n for n, c in names if c == "wstrzymal_sie"],
            "brak_glosu": [n for n, c in names if c == "brak_glosu"],
            "nieobecni": [n for n, c in names if c == "nieobecni"],
        }, "counts": {}})
    return votes


def parse_pdf(data, session_date):
    try:
        txt = "\n".join(_lines(data))
    except Exception:
        return []
    if "Wyniki imienne" in txt:
        return _parse_format_a(data, session_date)
    if "Wynik głosowania nr" in txt:
        return _parse_format_b(data, session_date)
    return []


# ---------------------------------------------------------------------------
# 3. Główna kolekcja: sesje -> pliki -> PDF-y -> głosowania
# ---------------------------------------------------------------------------
def collect_all(sessions, cache_dir=None):
    records = []
    for s in sessions:
        try:
            data = fetch(s["url"], cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] pdf {s['fileid']}: {e}")
            continue
        vs = parse_pdf(data, s["date"])
        if not vs:
            print(f"  [warn] {s['numeral']!r} {s['date']}: nie sparsowano żadnego głosowania")
        for v in vs:
            rec = dict(v)
            rec["session_num"] = s["roman"]
            rec["cluster_key"] = s["date"]
            records.append(rec)
        print(f"  {s['roman']:8s} {s['date']} votes={len(vs)}")
    return records


# ---------------------------------------------------------------------------
# 4. Budowa wyjścia (to samo co konin)
# ---------------------------------------------------------------------------
def _canonical_rec(rec):
    for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
        rec["named"][k] = [_canonical_name_b(n) for n in rec["named"].get(k, [])]
    return rec


def _compute_consensus(all_votes):
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

    print("=== Scraper Rada Miasta Siemianowice Śląskie (Finn.pl /bipkod/35184442) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kadencji: {len(sessions)}")

    records = collect_all(sessions, cache_dir)
    print(f"  Razem glosowan: {len(records)}")

    # Uwaga: nazwy są już kanonizowane na etapie parsowania (_canonical_name →
    # "Imię Nazwisko"); NIE dokonujemy tu ponownej kanonizacji (podwójna
    # zamiana "Nazwisko Imię" ↔ "Imię Nazwisko" zepsułaby nazwy).

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
