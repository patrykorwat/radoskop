#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Pisz — imienne głosowania Rady Miejskiej w Piszu (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Piszu (bip.pisz.hi.pl, własny CMS PHP "index.php?k=N").
Rada Miejskia → "Imienny wykaz głosowań" (kategoria k=1133) publikuje per sesja
artykuł z załącznikiem PDF "Imienny wykaz głosowań" (generowany przez eSesja,
TEKSTOWY — pełna warstwa tekstowa) z głosowaniami imiennymi per radny.

Format PDF (klasyczny 2-kolumnowy eSesja "wykaz imienny"):
  <NN> <Sesja> ... / Głosowanie / <K.> <tytuł> / Data głosowania ...
  Liczba uprawnionych <N> | Głosy za <a> | Głosy przeciw <p> | Głosy wstrzymujące się <w>
  Obecni niegłosujący <g> | Liczba nieobecnych <nb>
  Uprawnieni do głosowania
  <Lp> <Nazwisko i imię> <GŁOS>   <Lp> <Nazwisko i imię> <GŁOS>   (dwie kolumny / wiersz)
  ...
Głosy: ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEOBECN(A|Y|E) / OBECN(A|Y) [= obecny niegłosujący].

ZAGADNIENIA PARSOWANIA (pokrywane):
  * 2-kolumnowy układ — rekordy rozdzielane na "Głos" (keyword) + numer Lp w wersjach
    z glitchami OCR (np. Lp "ii.", "7-", "9'", "_2_0_."); tokenizer rekurencyjny
    dzieli na słowo-głos.
  * Nagłówki "Lp Nazwisko i imię Głos Lp. Nazwisko i imię Głos" (warianty OCR "p-"/"LP-")
    są odfiltrowywane (zawierają "nazwisko").
  * Skład Rady ZMIENIAŁ SIĘ w trakcie kadencji: Ciecierska Marzena (wczesne sesje)
    zastąpiona przez Krośniewskiego; sesje miał 20-21 radnych. Identyfikacja radnego
    po NAZWISKU (fuzzy, tolerancja szumów OCR typu "Zadroga"→"Zad noga", "Oiender"),
    NIE po Lp.
  * "OBECNY/OBECNA" = obecny niegłosujący (potwierdzone nagłówkiem "Obecni niegłosujący N")
    -> kategoria brak_glosu w named_votes.

Adresy:
  Kategoria:  https://bip.pisz.hi.pl/index.php?k=1133
  Artykuł:    https://bip.pisz.hi.pl/index.php?wiad={id}
  PDF:        https://bip.pisz.hi.pl/download.php?id={att}
  Skład rady: https://bip.pisz.hi.pl/index.php?k=53

Użycie:
    python scrape_pisz.py --output docs/data.json --profiles docs/profiles.json
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

import difflib
import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.pisz.hi.pl"
CAT = 1133          # Rada Miejska -> Imienny wykaz głosowań
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Radni IX kadencji (Skład osobowy Rady Miejskiej w Piszu, BIP k=53) + Ciecierska
# (wczesne sesje, zastąpiona przez Krośniewskiego). Identyfikacja po nazwisku.
ROSTER = [
    "Bobko Anna Małgorzata", "Czerwiński Krzysztof", "Górski Jarosław Bogdan",
    "Kaczkowski Dariusz", "Konopa Zuzanna", "Krawczyk Adam", "Krośniewski Robert Czesław",
    "Olender Dariusz", "Pardo Agnieszka", "Pietrzyk Łukasz", "Roszczypała Jolanta",
    "Sawicka Aneta Agnieszka", "Sparzak Maciej", "Stawecki Wojciech Tomasz",
    "Szmigiel Małgorzata", "Szpanko Mariusz", "Szymborski Andrzej Janusz", "Święconek Karol",
    "Trupacz Mariusz", "Zadroga Andrzej", "Zuzga Sebastian", "Ciecierska Marzena",
]


def _norm(s: str) -> str:
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


_ROSTER_NORM = [(_norm(n), _norm(" ".join(n.split()[:2])), n) for n in ROSTER]


def _resolve(name: str) -> str | None:
    n = _norm(name)
    for nf, nt, disp in _ROSTER_NORM:
        if n == nf or n == nt:
            return disp
    best, br = None, 0.0
    for nf, nt, disp in _ROSTER_NORM:
        r = max(difflib.SequenceMatcher(None, n, nf).ratio(),
                difflib.SequenceMatcher(None, n, nt).ratio())
        if r > br:
            br, best = r, disp
    return best if br >= 0.70 else None


REQ_DELAY = 0.4
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=50, verify=False)
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
# 1. Kolekcja sesji
# ---------------------------------------------------------------------------

_MONTH_PL = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
             'lipca': 7, 'sierpnia': 8, 'wrzesnia': 9, 'pazdziernika': 10, 'listopada': 11, 'grudnia': 12}


def _parse_date(pod: str) -> str | None:
    m = re.search(r'(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', pod, re.I)
    if m:
        mon_raw = m.group(2).lower()
        mon_norm = "".join(c for c in unicodedata.normalize("NFKD", mon_raw)
                           if not unicodedata.combining(c))
        mon = _MONTH_PL.get(mon_raw) or _MONTH_PL.get(mon_norm)
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return None


def collect_sessions(cache_dir):
    html = fetch(f"{BIP}/index.php?k={CAT}", cache_dir)
    arts = re.findall(r'<article[^>]*id="zajawka-(\d+)"[^>]*>(.*?)</article>', html, re.S)
    sessions = []
    for aid, body in arts:
        t = re.search(r'<h2[^>]*>(.*?)</h2>', body, re.S)
        title = re.sub(r'<[^>]+>', '', t.group(1)).strip() if t else ''
        d = re.search(r'zajawka__podtytul[^>]*>(.*?)</div>', body, re.S)
        pod = re.sub(r'<[^>]+>', '', d.group(1)).strip() if d else ''
        m = re.match(r'([IVXLCDM]+)\s+Sesja', title)
        if not m:
            continue
        date = _parse_date(pod)
        if not date or date < KAD_START:
            continue
        sessions.append({"art_id": aid, "num": m.group(1), "date": date})
    for s in sessions:
        ah = fetch(f"{BIP}/index.php?wiad={s['art_id']}", cache_dir)
        gm = re.search(r'href="(download\.php\?id=(\d+))"[^>]*>([^<]*?Imienny wykaz[^<]*)</a>', ah, re.S | re.I)
        if not gm:
            gm = re.search(r'href="(download\.php\?id=(\d+))"[^>]*>\s*([^<]*?wykaz[^<]*)</a>', ah, re.S | re.I)
        s["pdf_id"] = gm.group(2) if gm else None
    sessions = [s for s in sessions if s.get("pdf_id")]
    sessions.sort(key=lambda s: s["date"])
    return sessions


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF
# ---------------------------------------------------------------------------

_VKW = re.compile(r'(ZA|PRZECIW|WSTRZYMUJ[EĘ]\s+SI[EĘ]|NIEOBECN[AOE]?|OBECN[AOE]?)')
_VK = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
       'WSTRZYMUJE SIĘ': 'wstrzymal_sie', 'NIEOBECNA': 'nieobecni', 'NIEOBECNY': 'nieobecni',
       'NIEOBECNE': 'nieobecni', 'NIEOBECN': 'nieobecni',
       'OBECNY': 'brak_glosu', 'OBECNA': 'brak_glosu', 'OBECN': 'brak_glosu'}


def _split_records(up: str):
    """Rekurencyjny tokenizer na słowie-głos; zwraca [(name, vote_tag)]."""
    raw = re.sub(r'(?m)^\s*[-\u2013\u2014]\s*$', '', up)
    raw = re.sub(r'\s*Wydrukowano.*$', '', raw, flags=re.S).replace('\n', ' ')
    # Utnij wszystko PRZED pierwszym rekordem z listy radnych (nagłówek "Lp Nazwisko
    # i imię Głos Lp. Nazwisko i imię Głos" i nagłówki użycia) — Lp1 jest zawsze
    # Bobko Anna; nagłówek nie zawiera żadnego nazwiska radnego.
    first = len(raw)
    for _, _, disp in _ROSTER_NORM:
        key = " ".join(disp.split()[:2])
        i = raw.find(key)
        if i != -1 and i < first:
            first = i
    if first < len(raw):
        raw = raw[first:]
    raw = re.sub(r'^[\s\-_–]+', '', raw)
    out = []
    while True:
        raw = raw.lstrip()
        raw = re.sub(r'^\s*(?:[IVXLCivxlc]{1,5}|\d{1,2})[.,\'’`°\-\u2013\u2014]?\s*', '', raw)
        if not raw.strip():
            break
        m = _VKW.search(raw)
        if not m:
            break
        nm = raw[:m.start()].strip()
        tag = " ".join(m.group(1).split())
        raw = raw[m.end():]
        if not nm or 'nazwisko' in _norm(nm):
            continue  # nagłówek tabeli (fallback)
        out.append((nm, tag))
    return out


def _parse_vote_block(text: str, warn):
    tm = re.search(r'(?:^|\n)\s*(.*?)\n\s*Typ głosowania', text, re.S)
    topic = re.sub(r'\s+', ' ', tm.group(1)).strip() if tm else 'Głosowanie'
    topic = re.sub(r'^\d+\s*\.?\s*', '', topic)
    topic = re.sub(r'^\d+\s+', '', topic)
    # nagłówek-Liczby do walidacji (zero = brak cyfry po słowie, lub litera 'O')
    def _val(kw):
        m = re.search(re.escape(kw) + r'\s*([0-9O]+)?', text)
        if not m or not m.group(1):
            return 0
        return int(m.group(1).replace('O', '0'))
    hdr = {'za': _val('Głosy za'), 'przeciw': _val('Głosy przeciw'),
           'wz': _val('Głosy wstrzymujące się'), 'ng': _val('Obecni niegłosujący'),
           'nb': _val('Liczba nieobecnych')}
    named = {'za': [], 'przeciw': [], 'wstrzymal_sie': [], 'brak_glosu': [], 'nieobecni': []}
    for nm, tag in _split_records(text.split('Uprawnieni do głosowania')[-1]):
        rname = _resolve(nm)
        key = _VK.get(tag)
        if rname and key:
            named[key].append(rname)
    # walidacja vs nagłówek
    if (len(named['za']) != hdr['za'] or len(named['przeciw']) != hdr['przeciw']
            or len(named['wstrzymal_sie']) != hdr['wz'] or len(named['brak_glosu']) != hdr['ng']):
        warn.append(f"  [MISMATCH] za {len(named['za'])}/{hdr['za']} przeciw {len(named['przeciw'])}/{hdr['przeciw']} "
                    f"wz {len(named['wstrzymal_sie'])}/{hdr['wz']} ng {len(named['brak_glosu'])}/{hdr['ng']} :: {topic[:55]}")
    return {'topic': topic, **named}


def parse_report_pdf(data: bytes, warn):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full = '\n'.join((p.extract_text() or "") for p in pdf.pages)
    parts = re.split(r'\n\s*Głosowanie\s*\n', full)
    return [_parse_vote_block(p, warn) for p in parts[1:]]


# ---------------------------------------------------------------------------
# 3. Budowanie outputu (schemat Radoskopa)
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["num"],
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec["num"],
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

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {
            "name": name, "club": "", "district": None,
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
            "name": c["name"], "club": "", "district": None,
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

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": {},
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
        d = rec.get("date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal_sie" if cat == "wstrzymal_sie" else "nieobecny" if cat == "nieobecni" else "brak"
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
                    "club": "", "has_voting_data": True,
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
    warn = []

    print("=== Scraper Rada Miejska Pisz (bip.pisz.hi.pl, k=1133 Imienny wykaz głosowań) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kad.: {len(sessions)}")

    records = []
    for s in sessions:
        try:
            data = fetch(f"{BIP}/download.php?id={s['pdf_id']}", cache_dir, binary=True)
            votes = parse_report_pdf(data, warn)
        except Exception as e:
            print(f"  [err] sesja {s['num']} {s['date']}: {e}")
            continue
        for v in votes:
            records.append({"date": s["date"], "num": s["num"], "topic": v["topic"],
                            "named": {k: v[k] for k in ("za", "przeciw", "wstrzymal_sie",
                                                        "brak_glosu", "nieobecni")}})
        print(f"  sesja {s['num']} {s['date']}: {len(votes)} głosowań")
        time.sleep(0.2)

    print(f"  RAZEM głosowań imiennych: {len(records)} w {len(sessions)} sesjach")
    if warn:
        print(f"  UWAGA — dopasowanie do nagłówka niepełne ({len(warn)}):")
        for w in warn[:20]:
            print(w)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    print("  Zapisano:", args.output, "(data.json + kadencja-*.json + profiles.json)")


if __name__ == "__main__":
    main()
