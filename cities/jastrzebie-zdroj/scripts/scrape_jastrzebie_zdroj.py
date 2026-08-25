#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Jastrzębie-Zdrój — imienne głosowania Rady Miejskiej.

Źródło: BIP Miasta Jastrzębie-Zdrój (bip.jastrzebie.pl, CMS Logonet).
Rada Miejska w Jastrzębiu-Zdroju (IX kadencja 2024-2029) publikuje w kategorii
"Wyniki głosowań" (/artykuly/wyniki-glosowan-2) per-sesyjne raporty
"Raport z głosowań" / "Wyniki z głosowań" — PDF-y generowane przez app.esesja.pl
z wynikami imiennymi: ZA / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI
per radny, temat głosowania, sesja i godzina głosowania. Dla każdej sesji jest
jeden PDF z N głosowaniami (każde = jeden punkt uchwały).

Older (2024-10 i wcześniejsze) protokoły są narracyjne (bez tabeli imiennej) —
brakuje im raportów z głosowań. Dane IDą od IX Sesji nadzwyczajnej (2024-11-13).
Dwa formaty raportów eSesja: nowy (2025-03+, sekcje 'ZA (N)' + listy nazwisk)
i stary (2024-11..2025-02, płaska lista 'Nazwisko (KATEGORIA)' per radny).
Zakres danych: 26 sesji (2024-11 .. 2026-07), 345 głosowań imiennych.

Kluby radnych: kuratorowane z BIP (protokoły) — PENDING, patrz config.json.

Użycie:
    python scrape_jastrzebie_zdroj.py --output docs/data.json --profiles docs/profiles.json
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

BIP = "https://bip.jastrzebie.pl"
LIST_CAT = "wyniki-glosowan-2"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PAGE_CAP = 30

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    "wrzesnia": 9, "pazdziernika": 10,
}


def _norm_mon(mon: str):
    mon = (mon or "").lower()
    if mon in MONTHS_PL:
        return mon
    if mon.startswith("wrze"):
        return "września"
    if mon.startswith("pazd"):
        return "października"
    return None


def _date_from_href(href: str):
    """'/artykul/...-z-23-lipca-2026-roku' -> '2026-07-23'."""
    m = re.search(r"z-(\d{1,2})-([a-ząćęłńóśźż]+)-(\d{4})-roku", href)
    if not m:
        return None
    d, mon, y = int(m.group(1)), _norm_mon(m.group(2)), int(m.group(3))
    if mon:
        return f"{y:04d}-{MONTHS_PL[mon]:02d}-{d:02d}"
    return None


def _roman_from_href(href: str):
    """'/artykul/...-viii-sesji-...' -> 'VIII'."""
    m = re.search(r"-([ivxlcdm]+)-sesji", href)
    return m.group(1).upper() if m else ""

ORDER_SRC = ['ZA', 'PRZECIW', 'WSTRZYMUJĘ SIĘ', 'BRAK GŁOSU', 'NIEOBECNI']
CNTKEY = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
          'BRAK GŁOSU': 'brak_glosu', 'NIEOBECNI': 'nieobecni'}
CAT_MAP = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
           'WSTRZYMAŁ SIĘ': 'wstrzymal_sie', 'WSTRZYMUJE SIĘ': 'wstrzymal_sie',
           'BRAK GŁOSU': 'brak_glosu', 'NIEOBECNI': 'nieobecni'}


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


# ---- Kluby (PENDING — kuratorowane) ----
# Z protokołów VI Sesji (2026-05-28): Klub Radnych Koalicja Samorządowa (Foksowicz),
# Klub Radnych Górnicze Miasto Jastrzębie-Zdrój (Rosińska),
# Klub Radnych Prawo i Sprawiedliwość (Sławik). Pełne przypisania do ustalenia.
CLUBS_META = {
    "KS": {"name": "Koalicja Samorządowa", "color": "#0ea5e9",
           "bg": "rgba(14,165,233,0.12)", "avatar_bg": "#0369a1"},
    "GM": {"name": "Górnicze Miasto Jastrzębie-Zdrój", "color": "#16a34a",
           "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "NZ": {"name": "Niezrzeszeni", "color": "#6b7280",
           "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}
# klub leaderzy z protokołów; reszta radnych NZ do czasu skuratorowania
CLUB_ASSIGN = {
    "Roman Foksowicz": "KS", "Iwona Rosińska": "GM", "Tadeusz Sławik": "PiS",
}


def _club_of(name):
    return CLUB_ASSIGN.get(name, "NZ")


REQ_DELAY = 0.4
_LAST_REQ = 0.0


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
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=45, verify=False)
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


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji z kategorii "Wyniki głosowań"
# ---------------------------------------------------------------------------
def collect_sessions(cache_dir=None):
    """Zwraca [{url, att, label, date, roman}] — sesje IX kadencji z raportem głosowań (PDF).

    Data i numer sesji pochodzą z URL artykułu ('...-z-23-lipca-2026-roku' /
    '...-viii-sesji-...'), bo nagłówki PDF bywają niejednolite (Sesja
    Nadzwyczajna / zwyczajna, 'w dniu 27 marca 2025' bez ISO itd.)."""
    out = []
    seen = set()
    for page in range(1, PAGE_CAP + 1):
        url = f"{BIP}/artykuly/{LIST_CAT}?page={page}&limit=100"
        try:
            html = fetch(url, cache_dir)
        except Exception as e:
            print(f"    [warn] page {page}: {e}")
            break
        arts = re.findall(r'href="(/artykul/wyniki[^"]+)"', html)
        arts = [a for a in arts if 'user_attachments' not in a]
        new = [a for a in dict.fromkeys(arts) if a not in seen]
        if not new:
            break
        for a in new:
            seen.add(a)
            date = _date_from_href(a)
            roman = _roman_from_href(a)
            if not date or date < KAD_START:
                continue
            full = BIP + a
            try:
                ah = fetch(full, cache_dir)
            except Exception:
                continue
            # attachment PDF raportu głosowań
            atts = re.findall(r'href="(/attachments/\d+/download/[^"]+)"', ah)
            atts = [x for x in atts if 'user_attachments' not in x]
            if not atts:
                continue
            out.append({"url": full, "att": BIP + atts[0], "label": a,
                        "date": date, "roman": roman})
        print(f"    page {page}: sessions collected total {len(out)}")
        if page >= PAGE_CAP:
            break
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF raportu głosowań
# ---------------------------------------------------------------------------
def _clean_name(n):
    n = n.replace("\u2013", "-").replace("\u2014", "-")
    n = n.replace("-\u00a0", "-").replace("\u00a0", " ")
    n = re.sub(r'\s*-\s+', '-', n)
    n = " ".join(n.split())
    return n.strip()


def _pdf_lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        lines = []
        for p in pdf.pages:
            for l in (p.extract_text() or "").split("\n"):
                l = l.strip()
                if l == "Wygenerowano za pomocą app.esesja.pl":
                    continue
                if re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', l):
                    continue
                lines.append(l)
    return lines


def parse_report_pdf(data, session_date=None, session_num=None):
    lines = _pdf_lines(data)
    # PDF header jako fallback (gdyby URL nie niósł daty); preferowany date z URL.
    if not session_date:
        for l in lines:
            m = re.search(r'Sesja (?:nadzwyczajna |Nadzwyczajna |zwyczajna |Zwyczajna )?Rady Miasta w dniu (\d{4}-\d\d-\d\d)', l)
            if m:
                session_date = m.group(1)
                break
        else:
            for l in lines:
                m = re.search(r'Sesja\s+\S*\s*Rady Miasta w dniu (\d{4}-\d\d-\d\d)', l)
                if m:
                    session_date = m.group(1)
                    break
    if not session_num:
        for l in lines:
            nm = re.search(r'^\s*([IVXLCDM]+)\s+Sesja', l)
            if nm:
                session_num = nm.group(1)
                break
    block_starts = [i for i, l in enumerate(lines)
                    if re.match(r'^\s*\d+\.\s*Głosowano w sprawie:', l)]
    votes = []
    for bi, start in enumerate(block_starts):
        end = block_starts[bi + 1] if bi + 1 < len(block_starts) else len(lines)
        vt = _parse_block(lines[start:end], session_date, session_num)
        if vt:
            votes.append(vt)
    return votes, session_date, session_num


def _parse_block(lines, session_date, session_num):
    m = re.match(r'^\s*\d+\.\s*Głosowano w sprawie:\s*(.*)$', lines[0])
    topic = None
    time_ = None
    if m:
        rest = m.group(1)
        mm = re.match(r'^(.*?)\s*-\s*czas głosowania:\s*(.*)$', rest)
        if mm:
            topic = mm.group(1).strip().rstrip('.').strip()
            time_ = mm.group(2).strip()
        else:
            topic = rest.strip().rstrip('.').strip()
    counts = {}
    for l in lines:
        rm = re.match(r'^\s*ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),'
                      r'\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),'
                      r'\s*NIEOBECNI:\s*(\d+)\s*$', l)
        if rm:
            counts = {'za': int(rm.group(1)), 'przeciw': int(rm.group(2)),
                      'wstrzymal_sie': int(rm.group(3)), 'brak_glosu': int(rm.group(4)),
                      'nieobecni': int(rm.group(5))}
            break
    blocktext = " ".join(lines[1:])
    c = blocktext.find('Uczestnictwo w głosowaniach')
    if c >= 0:
        blocktext = blocktext[:c]
    im = blocktext.find('Wyniki imienne:')
    if im < 0:
        return None
    body = blocktext[im + len('Wyniki imienne:'):]
    named = _parse_imienne(body, counts)
    return {"session_date": session_date, "session_num": session_num,
            "topic": topic, "time": time_, "counts": counts, "named": named}


_HDR_RE = re.compile(r'(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\(\s*(\d+)\s*\)')
_ANN_RE = re.compile(
    r'([A-ZĄĆĘŁŃÓŚŹŻ][^\.\(\)]*?)\s*\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMAŁ SIĘ|'
    r'WSTRZYMUJE SIĘ|BRAK GŁOSU|NIEOBECNI)\)')


def _parse_imienne(body, counts):
    """Z 'Wyniki imienne:' dalej wyciąga nazwiska per kategoria.

    Dwa formaty raportów eSesja:
      * nowy (2025-03+): sekcje nawiasowe 'ZA (17)' z listą nazwisk pod spodem;
      * stary (2024-11..2025-02): płaska lista 'Nazwisko (ZA), ...' — jedna
        adnotacja per radny.
    Decyzja po zgodności z nagłówkowymi liczbami (counts) — źródło prawdy.
    """
    named = {k: [] for k in cat_keys()}
    # nowy format: sekcje 'CAT (N)'
    ms = list(_HDR_RE.finditer(body))
    if ms:
        nw = {k: [] for k in cat_keys()}
        for mi, mm in enumerate(ms):
            cat = CAT_MAP.get(mm.group(1))
            seg_end = ms[mi + 1].start() if mi + 1 < len(ms) else len(body)
            seg = body[mm.end():seg_end]
            for name in seg.split(","):
                cn = _clean_name(name)
                if cn:
                    nw[cat].append(cn)
            if mm.group(2) == "0":
                nw[cat] = []
        if all(len(nw[c]) == counts.get(c, -1) for c in cat_keys()):
            return nw
        named = nw  # fallback: zachowaj najlepsze co mamy
    # stary format: adnotacje 'Nazwisko (KATEGORIA)'
    for mm in _ANN_RE.finditer(body):
        name = _clean_name(mm.group(1))
        cat = CAT_MAP.get(mm.group(2))
        if name and cat:
            named[cat].append(name)
    return named


def cat_keys():
    return ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]


# ---------------------------------------------------------------------------
# 3. Build output (kadencja + data.json + profiles.json)
# ---------------------------------------------------------------------------
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
        for cat in cat_keys():
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
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

    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for n in names:
                    councillor_sess[n].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
            "activity": None,
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


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
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
                cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {
                KADENCJA_ID: {
                    "club": _club_of(name), "has_voting_data": True,
                    "has_activity_data": False, "frekwencja": round(frekw, 1),
                    "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                    "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                    "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                    "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                    "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                    "former": False, "mid_term": False,
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

    print("=== Scraper Rada Miejska Jastrzębie-Zdrój (bip.jastrzebie.pl) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji z raportem głosowań: {len(sessions)}")

    records = []
    mismatch = 0
    for s in sessions:
        try:
            data = fetch(s["att"], cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['label']} pdf: {e}")
            continue
        votes, sd, sn = parse_report_pdf(data, s.get("date"), s.get("roman"))
        sd = sd or s.get("date")
        if not sd or sd < KAD_START:
            continue
        for v in votes:
            ok = all(len(v["named"].get(cat, [])) == v["counts"].get(cat, -1)
                     for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"))
            if not ok:
                mismatch += 1
            records.append(v)
        print(f"  {s['label'][:55]:55s} {sd} votes={len(votes)}")

    print(f"  Razem glosowan: {len(records)} (mismatch count vs naglowek: {mismatch})")

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
