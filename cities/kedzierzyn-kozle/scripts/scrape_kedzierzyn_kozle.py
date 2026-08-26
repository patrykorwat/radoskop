#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Kędzierzyn-Koźle — imienne głosowania Rady Miasta Kędzierzyn-Koźle
(IX kadencja 2024-2029).

Źródło: BIP Miasta (własny CMS, https://bip.kedzierzynkozle.pl — NIE
bip.kedzierzynkozle.pl z myślnikiem, ten jest martwy). Rada Miasta publikuje w
kategorii „Rada Miasta → Protokoły i głosowania z sesji Rady Miasta”
(/artykuly/53/protokoly-i-glosowania-z-sesji-rady-miasta, paginacja /artykuly/53/{page}/10/...)
per-sesyjne protokoły. Do każdego artykułu protokołu dołączone są (kolejno):
  * Ramowy projekt protokołu ...  (pomijany)
  * Projekt protokołu {nr} ...    (z głosowaniami imiennymi)
  * Protokół {nr} ...             (finalny, z głosowaniami imiennymi)
  * (czasem) wyniki_glosowania_N.pdf
Parser wybiera z załączników ten z nazwą zawierającą „protokołu” (pomijając
„Ramowy”), a jeśli wybrany plik nie zawiera tabel imiennych — próbuje kolejne.

Format imiennych wyników w protokole (tekstowy, pdfplumber):
  Wyniki głosowania
  ZA: 21, PRZECIW: 0, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 1, NIEOBECNI: 1
  Wyniki imienne:
  ZA (21)
  Agata Blachucik, Ewa Czubek, Grzegorz Draguła, ... (nazwiska oddzielone przecinkami,
  mogą się łamać między wierszami)
  PRZECIW (4)
  ...
  WSTRZYMUJĘ SIĘ (4)
  ...
  BRAK GŁOSU (1)
  ...
  NIEOBECNI (1)
  ...

Radni i kluby: 23 radnych IX kadencji z BIP „Skład osobowy Rady Miasta
Kędzierzyn-Koźle na kadencję 2024-2029” (/artykuly/1581/). Kluby: Klub radnych
Sabiny Nowosielskiej – Koalicja Obywatelska (15), KW/Klub radnych Prawo i
Sprawiedliwość (4), KWW Niezależni z Markiem Piaseckim (4).

Użycie:
    python scrape_kedzierzyn_kozle.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
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

SITE = "https://bip.kedzierzynkozle.pl"
CAT = "artykuly/53/protokoly-i-glosowania-z-sesji-rady-miasta"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.6
_LAST_REQ = 0.0
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"}

# ---- Kluby radnych (kuratorowane z BIP /artykuly/1581 skład osobowy 2024-2029) ----
CLUBS_META = {
    "KO": {"name": "Koalicja Obywatelska", "color": "#f59e0b",
           "bg": "rgba(245,158,11,0.12)", "avatar_bg": "#b45309"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "NZP": {"name": "Niezależni z Markiem Piaseckim", "color": "#16a34a",
            "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
}

# 23 radnych IX kadencji (imię nazwisko) -> klub
CLUB_ASSIGN = {
    # Klub radnych Sabiny Nowosielskiej – Koalicja Obywatelska (15)
    "Hanna Białas": "KO", "Ewa Czubek": "KO", "Marzanna Gądek-Radwanowska": "KO",
    "Wojciech Jagiełło": "KO", "Tomasz Kiel": "KO", "Andrzej Kopacki": "KO",
    "Elżbieta Kozakiewicz": "KO", "Anna Kras": "KO", "Małgorzata Lipczyńska": "KO",
    "Halina Mińczuk": "KO", "Michał Nowak": "KO", "Adam Oczoś": "KO",
    "Ewa Odulińska": "KO", "Ireneusz Wiśniewski": "KO", "Marcin Wołyniec": "KO",
    # KW / Klub radnych Prawo i Sprawiedliwość (4)
    "Agata Blachucik": "PiS", "Jakub Gładysz": "PiS",
    "Katarzyna Kukolka-Bogocz": "PiS", "Fabian Pszon": "PiS",
    # KWW Niezależni z Markiem Piaseckim (4)
    "Grzegorz Draguła": "NZP", "Wiesław Fąfara": "NZP",
    "Jacek Król": "NZP", "Marek Piasecki": "NZP",
}
ROSTER_NAMES = set(CLUB_ASSIGN.keys())  # kanoniczne "Imię Nazwisko"


def _norm(s):
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def club_of(name):
    return CLUB_ASSIGN.get(name, "NZP")


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
    raw = raw.strip().rstrip("*").strip()
    parts = raw.split()
    if not parts:
        return ""
    return " ".join(p[:1].upper() + p[1:].lower() if p else "" for p in parts)


def match_roster(name):
    """Dopasuj nazwisko z głosowania do kanonicznego 'Imię Nazwisko' z rejestru."""
    if name in ROSTER_NAMES:
        return name
    n = _norm(name)
    for rn in ROSTER_NAMES:
        if _norm(rn) == n:
            return rn
    return name


# ---- HTTP z cache ----
def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url, cache_dir=None, binary=False, tries=3):
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    for i in range(tries):
        try:
            _rate()
            resp = requests.get(url, headers=UA, timeout=60, verify=False)
            resp.raise_for_status()
            if binary:
                data = resp.content
                if cache_dir is not None:
                    (cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".bin")).write_bytes(data)
                return data
            resp.encoding = "utf-8"
            data = resp.text
            if cache_dir is not None:
                (cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")).write_text(data, encoding="utf-8")
            return data
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")


# ---- 1. Kolekcja sesji (artykułów protokołów) IX kadencji ----
_MONTHS = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
           'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'pazdziernika': 10,
           'października': 10, 'listopada': 11, 'grudnia': 12,
           'marca': 3, 'czerwca': 6}
_ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}


def _roman_to_int(roman):
    n, prev = 0, 0
    for ch in reversed(roman.upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n


def _date_from_title(title):
    m = re.search(r'w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})', title)
    if not m:
        return ""
    d, mo, y = m.group(1), m.group(2).lower(), m.group(3)
    if mo not in _MONTHS:
        return ""
    return f"{y}-{_MONTHS[mo]:02d}-{int(d):02d}"


def _roman_from_title(title):
    m = re.search(r'protoko[łl]u(?:\s+nr)?\s+([IVXLCDM]{1,6})/', title)
    return m.group(1) if m else ""


def collect_sessions(cache_dir=None):
    out, seen = [], set()
    for page in range(1, 40):
        url = f"{SITE}/{CAT}" if page == 1 else f"{SITE}/artykuly/53/{page}/10/protokoly-i-glosowania-z-sesji-rady-miasta"
        html = fetch(url, cache_dir)
        found = 0
        for m in re.finditer(r'href="(https://bip\.kedzierzynkozle\.pl/artykul/53/(\d+)/[^"]+)"', html):
            url2, art = m.group(1), m.group(2)
            if url2 in seen:
                continue
            seen.add(url2)
            # tytuł = tekst kotwicy (bezpośrednio po href)
            am = re.search(re.escape(url2) + r'[^>]*>\s*([^<]{5,220})', html)
            title = am.group(1).strip() if am else ""
            date = _date_from_title(title)
            if not date or date < KAD_START:
                continue
            found += 1
            out.append({"art_id": art, "title": title, "date": date,
                        "roman": _roman_from_title(title)})
        if found == 0:
            break
        time.sleep(0.5)
    # sortuj chronologicznie
    out.sort(key=lambda s: s["date"])
    # sesje bez numeru rzymskiego w tytule (np. 'Protokół z sesji ... 31 marca 2026') —
    # przypisz po porządku chronologicznym
    for i, s in enumerate(out, 1):
        if not s["roman"]:
            s["roman"] = _int_to_roman(i)
    return out


def _int_to_roman(n):
    vals = [(10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]
    out = ""
    for v, r in vals:
        while n >= v:
            out += r
            n -= v
    return out


# ---- 2. Wybór załącznika protokołu + parsowanie PDF ----
def protokol_urls_for_article(art_id, cache_dir=None):
    html = fetch(f"{SITE}/artykul/53/{art_id}", cache_dir)
    cands = []
    for m in re.finditer(r'href="(https://bip\.kedzierzynkozle\.pl/attachments/download/(\d+))"[^>]*>\s*([^<]{5,160})', html):
        url, aid, name = m.group(1), m.group(2), m.group(3).strip()
        name = re.sub(r'\s+', ' ', name)
        if "Ramowy" in name:
            continue
        if "protoko" in name.lower() or "protokół" in name.lower():
            cands.append((name, url))
    return cands  # [(name,url)] w kolejności występowania


def _lines_from_pdf(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        out = []
        for p in pdf.pages:
            for l in (p.extract_text() or "").split("\n"):
                ls = l.strip()
                if not ls:
                    continue
                if re.fullmatch(r"\d{1,3}", ls):  # numer strony (łapie się w listach nazwisk)
                    continue
                out.append(ls)
        return out


_CATMAP = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
           'WSTRZYMUJE SIE': 'wstrzymal_sie', 'BRAK GŁOSU': 'brak_glosu',
           'BRAK GLOSU': 'brak_glosu', 'NIEOBECNI': 'nieobecni'}
_CATS = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")
_HEAD_RE = re.compile(r'^(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIE|BRAK GŁOSU|BRAK GLOSU|NIEOBECNI)\s*\((\d+)\)\s*$', re.MULTILINE)
_AGG_RE = re.compile(r'ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)')
_NARR = re.compile(r'(poinformował|poddał|przeszedł|przyjęto|uchwalono|zakończono|głosowano|przystąpiono|otrzymano|zajął|wycofano)', re.I)


def _match_prefix(token):
    """Najdłuższy radny z rejestru, którego znormalizowane imię-nazwisko jest PREFIXEM tokenu."""
    n = _norm(token)
    for rn in sorted(ROSTER_NAMES, key=len, reverse=True):
        if n.startswith(_norm(rn)):
            return rn
    return None


def extract_names(seg, n):
    names = []
    joined_parts = []
    for ln in seg.split("\n"):
        ln = ln.strip().rstrip("*").strip()
        if not ln:
            continue
        if _NARR.search(ln) and not _match_prefix(ln.split(",")[0]):
            break
        joined_parts.append(ln)
    joined = " ".join(joined_parts)
    joined = re.sub(r'-\s+', '-', joined)  # złamanie 'Gądek- Radwanowska' -> 'Gądek-Radwanowska'
    for tok in joined.split(","):
        tok = tok.strip().rstrip("*").strip()
        if not tok:
            continue
        m = _match_prefix(tok)
        if m and m not in names:
            names.append(m)
        if len(names) >= n:
            break
    return names[:n]


def parse_pdf(data):
    try:
        lines = _lines_from_pdf(data)
    except Exception:
        return []
    full = "\n".join(lines)
    marks = [m for m in re.finditer(r"Wyniki głosowania", full)]
    votes = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(full)
        block = full[m.end():end]
        agg = _AGG_RE.search(block)
        imi = block.find("Wyniki imienne:")
        if imi < 0:
            continue
        imblock = block[imi + len("Wyniki imienne:"):]
        headers = list(_HEAD_RE.finditer(imblock))
        if not headers:
            continue
        named = {c: [] for c in _CATS}
        # zgoda: użyj liczby z nagłówka sekcji
        for hi, h in enumerate(headers):
            cat_key = h.group(1).upper()
            cat = _CATMAP.get(cat_key)
            if cat is None:
                continue
            n = int(h.group(2))
            seg_start = h.end()
            seg_end = headers[hi + 1].start() if hi + 1 < len(headers) else len(imblock)
            named[cat] = extract_names(imblock[seg_start:seg_end], n)
        if not (named["za"] or named["przeciw"] or named["wstrzymal_sie"]):
            continue
        topic = extract_topic(full, m.start())
        votes.append({"topic": topic, "named": named})
    return votes


def extract_topic(full, vote_pos):
    pre = full[:vote_pos]
    # numerowane punkty porządku obrad w tekście przed głosowaniem
    items = list(re.finditer(r'(?:^|\n)\s*(\d{1,2})\.\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\n]{10,300})', pre))
    if not items:
        return ""
    # preferuj ostatni punkt zaczynający się od czasownika decyzyjnego (podjęcie uchwały itp.)
    _DEC = re.compile(r'^(Podjęcie|Przyjęcie|Wybór|Zatwierdzenie|Udzielenie|Wyrażenie|Zmiana|Zmieniająca|Uchwała|Sprawozdanie|Absolutorium|Wotum|Program|Plan|Regulamin|Określenie|Ustalenie|Powołanie|Odwołanie|Nadanie|Rozpatrzenie|Uchwalenie|Zlecenie|Przystąpienie)')
    chosen = None
    for it in items:
        if _DEC.match(it.group(2).strip()):
            chosen = it
    if chosen is None:
        chosen = items[-1]
    topic = chosen.group(2).strip()
    topic = re.split(r'\.\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])', topic)[0].strip()
    topic = topic.rstrip(".").strip()
    if len(topic) > 250:
        topic = topic[:250].rsplit(" ", 1)[0]
    return topic


# ---- 3. Kolekcja wszystkich głosowań ----
def collect_all(sessions, cache_dir=None):
    records = []
    for s in sessions:
        try:
            cands = protokol_urls_for_article(s["art_id"], cache_dir)
            vs = []
            used = None
            for name, url in cands:
                pdf = fetch(url, cache_dir, binary=True)
                vs = parse_pdf(pdf)
                used = name
                if vs:
                    break
            if not vs:
                print(f"  [warn] {s['roman']:4s} {s['date']}: brak głosowań w protokołach")
                continue
        except Exception as e:
            print(f"  [warn] {s['roman']:4s} {s['date']}: {e}")
            continue
        for v in vs:
            rec = dict(v)
            rec["session_date"] = s["date"]
            rec["session_num"] = s["roman"]
            records.append(rec)
        print(f"  {s['roman']:4s} {s['date']} ({used}) votes={len(vs)}")
    return records


# ---- 4. Budowa wyjścia ----
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
    todo_total = len(_all_session_dates)
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        agg = NAME_AGG.get(name, {})
        all_sess = len(vd["sess"])
        frekw = 100.0 * all_sess / todo_total if todo_total else 0.0
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

    print("=== Scraper Rada Miasta Kędzierzyn-Koźle (BIP bip.kedzierzynkozle.pl /artykuly/53) ===")
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
