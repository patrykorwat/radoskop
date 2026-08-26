#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Police — imienne głosowania Rady Miejskiej w Policach.

Źródło: BIP na CMS Logonet (bip.police.pl). Rada Miejska w Policach (IX kadencja
2024-2029) publikuje per sesja artykuł "Protokół..." w kategorii
Rada Miejska → Protokoły z sesji Rady Miejskiej → {rok} z załącznikiem
"Raport ... głosowań" (PDF generowany przez app.esesja.pl, tekstowy) zawierającym
głosowania imienne per radny (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI).

Struktura:
  /artykuly/133/protokoly-z-sesji-rady-miejskiej  → podkategorie per rok (id)
  /artykuly/{cat}/{year}                          → lista artykułów-protokołów sesji
  /artykul/{cat}/{id}/...                          → artykuł sesji z załącznikami
  /attachments/download/{N}                        → PDF "Raport ... głosowań"

Raport ma dwa formaty (pokrywane oba):
  A (inline):  "Wyniki imienne: Imię Nazwisko (ZA), ..."
  B (bloki):   "Wyniki imienne:\nZA (N)\nImię Nazwisko, ...\nPRZECIW (N)..."

Kluby radnych skuratorowane z BIP "Kluby radnych IX kadencji" (stan 02.01.2026):
  KO = Koalicja Obywatelska, PiS = Prawo i Sprawiedliwość,
  PP = Projekt Police, NZ = Niezrzeszeni. Radni z wczesnych sesji 2024, którzy
  zostali zastąpieni (Ufniarz, Sosnowska, Pisański, Walczak), klub -> NZ.

Użycie:
    python scrape_police.py --output docs/data.json --profiles docs/profiles.json
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

BIP = "https://bip.police.pl"
PROTO_CAT = 133           # /artykuly/133/protokoly-z-sesji-rady-miejskiej
KAD_START = "2024-05-07"  # początek IX kadencji
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"


def _norm(s: str) -> str:
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = s.replace("\u00b3", "3")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


# ---- Kuratorowany skład rady + kluby (BIP "Kluby radnych IX kadencji", stan 02.01.2026) ----
# (src, display, club_key) — tutaj src == display (raporty podają "Imię Nazwisko").
ROSTER = [
    ("Ewa Anna Andruszko", "Ewa Anna Andruszko", "KO"),
    ("Waldemar Bubiłek", "Waldemar Bubiłek", "PiS"),
    ("Władysław Diakun", "Władysław Diakun", "NZ"),
    ("Wojciech Dudek", "Wojciech Dudek", "PP"),
    ("Artur Echaust", "Artur Echaust", "PP"),
    ("Wiesław Gaweł", "Wiesław Gaweł", "KO"),
    ("Ewa Ignaczak", "Ewa Ignaczak", "KO"),
    ("Adam Jarema", "Adam Jarema", "KO"),
    ("Krzysztof Kubistal", "Krzysztof Kubistal", "PP"),
    ("Stanisław Łabuz", "Stanisław Łabuz", "PiS"),
    ("Karina Mazurkiewicz", "Karina Mazurkiewicz", "NZ"),
    ("Marcin Michalak", "Marcin Michalak", "PP"),
    ("Jadwiga Molenda", "Jadwiga Molenda", "KO"),
    ("Żaneta Ostrowska", "Żaneta Ostrowska", "NZ"),
    ("Andrzej Rogowski", "Andrzej Rogowski", "NZ"),
    ("Małgorzata Siemianowska", "Małgorzata Siemianowska", "KO"),
    ("Andrzej Smoliński", "Andrzej Smoliński", "KO"),
    ("Anna Sobańska", "Anna Sobańska", "NZ"),
    ("Ireneusz Todorski", "Ireneusz Todorski", "KO"),
    ("Tomasz Tokarczyk", "Tomasz Tokarczyk", "PiS"),
    ("Elżbieta Wasikowska", "Elżbieta Wasikowska", "PiS"),
    # radni z wczesnych sesji IX kadencji, później zastąpieni (klub nieobecny w obecnym BIP -> NZ)
    ("Grzegorz Ufniarz", "Grzegorz Ufniarz", "NZ"),
    ("Magdalena Sosnowska", "Magdalena Sosnowska", "NZ"),
    ("Jakub Pisański", "Jakub Pisański", "NZ"),
    ("Damian Walczak", "Damian Walczak", "NZ"),
]
CLUB_BY_NORM = {_norm(s): c for s, _d, c in ROSTER}
DISPLAY_BY_NORM = {_norm(s): d for s, d, _c in ROSTER}
CLUB_BY_DISPLAY_NORM = {_norm(d): c for _s, d, c in ROSTER}

CLUBS_META = {
    "KO":  {"name": "Koalicja Obywatelska", "color": "#f97316",
            "bg": "rgba(249,115,22,0.12)", "avatar_bg": "#c2410c"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "PP":  {"name": "Projekt Police", "color": "#0ea5e9",
            "bg": "rgba(14,165,233,0.12)", "avatar_bg": "#0369a1"},
    "NZ":  {"name": "Niezrzeszeni", "color": "#6b7280",
            "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}

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
# 1. Kolekcja sesji (rok -> artykuły-protokoły -> PDF raportu głosowań)
# ---------------------------------------------------------------------------

_MONTH_PL = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
             'lipca': 7, 'sierpnia': 8, 'wrzesnia': 9, 'pazdziernika': 10, 'listopada': 11, 'grudnia': 12}


def discover_year_cats(cache_dir):
    """Zwraca {rok: cat_id} z kategorii Protokoły (>= 2024)."""
    html = fetch(f"{BIP}/artykuly/{PROTO_CAT}/protokoly-z-sesji-rady-miejskiej", cache_dir)
    cats = {}
    for m in re.finditer(r'href="[^"]*/artykuly/(\d+)/(\d{4})"', html):
        cat_id, year = m.group(1), int(m.group(2))
        if year >= 2024:
            cats[year] = cat_id
    # fallback — ustalone ID z 2026-08 (gdyby strona się zmieniła, nadal działają)
    cats.setdefault(2024, "771")
    cats.setdefault(2025, "822")
    cats.setdefault(2026, "874")
    return cats


def collect_sessions(cache_dir):
    """Zwraca listę dictów: {report_id, article_url, year}."""
    cats = discover_year_cats(cache_dir)
    out = []
    seen = set()
    for year in sorted(cats.keys()):
        cat_id = cats[year]
        html = fetch(f"{BIP}/artykuly/{cat_id}/{year}", cache_dir)
        for m in re.finditer(r'href="(https://bip\.police\.pl/artykul/(\d+)/(\d+)/[^"]*)"', html):
            aurl = m.group(1)
            if "mlodziezowej" in aurl:
                continue
            # tylko artykuły-protokoły sesji (w tej kategorii gównie protokoły)
            if not re.search(r'protokol', aurl, re.I):
                continue
            try:
                ah = fetch(aurl, cache_dir)
            except Exception:
                continue
            report = None
            for gm in re.finditer(r'<a[^>]+href="(https://bip\.police\.pl/attachments/download/(\d+))"[^>]*>(.*?)</a>', ah, re.S):
                label = re.sub(r"<[^>]+>", "", gm.group(3))
                if re.search(r"raport", label, re.I):
                    report = gm.group(2)
                    break
            if report is None:
                # fallback: atachment, który nie jest protokołem (oznaczony jako raport głosowań)
                for gm in re.finditer(r'<a[^>]+href="(https://bip\.police\.pl/attachments/download/(\d+))"[^>]*>(.*?)</a>', ah, re.S):
                    label = re.sub(r"<[^>]+>", "", gm.group(3))
                    if not re.search(r"protokol", label, re.I):
                        report = gm.group(2)
                        break
            if report and report not in seen:
                seen.add(report)
                out.append({"report_id": report, "article_url": aurl, "year": year})
    out.sort(key=lambda s: s["year"])
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF raportu głosowań
# ---------------------------------------------------------------------------

_VOTE_MAP = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
             'WSTRZYMUJE SIĘ': 'wstrzymal_sie', 'BRAK GŁOSU': 'brak_glosu', 'NIEOBECNI': 'nieobecni'}


def _norm_name(n):
    n = re.sub(r'\s+', ' ', n).strip(' .,;:')
    return n.strip()


def _parse_session_date(fulltext):
    m = re.search(r'w dniu\s+(\d{4})-(\d{2})-(\d{2})', fulltext)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r'(?:w dniu|z dnia)\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', fulltext, re.I)
    if m:
        mon_raw = m.group(2).lower()
        mon_norm = "".join(c for c in unicodedata.normalize("NFKD", mon_raw)
                           if not unicodedata.combining(c))
        mon = _MONTH_PL.get(mon_raw) or _MONTH_PL.get(mon_norm)
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', fulltext)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r'(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})', fulltext)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _cut(seg):
    for pat in [r'\n\s*Uczestnictwo', r'\n\s*Wygenerowano',
                r'\n\s*\d+\s*\.\s*[A-Za-zĄĆĘŁŃÓŚŹŻ]+\s+[A-Za-zĄĆĘŁŃÓŚŹŻ]+:']:
        m = re.search(pat, seg)
        if m:
            seg = seg[:m.start()]
    return seg.strip()


def _parse_votes(fulltext):
    lines = fulltext.split('\n')
    sec_starts = []
    for i, l in enumerate(lines):
        if re.match(r'^\s*\d+\.\s*(Głosowano|Głosowanie)', l):
            sec_starts.append(i)
    votes = []
    for k, start in enumerate(sec_starts):
        end = sec_starts[k + 1] if k + 1 < len(sec_starts) else len(lines)
        sec = '\n'.join(lines[start:end])
        tm = re.search(r'(?:Głosowano\s+w\s+sprawie:?\s*|Głosowanie\s+w\s+sprawie\s+)'
                       r'(.*?)(?:\s*-\s*czas głosowania|\s*czas głosowania)', sec, re.S | re.I)
        topic = _norm_name(tm.group(1)) if tm else f'Głosowanie {k + 1}'
        za, pr, wz, bg, nb = [], [], [], [], []
        im = re.search(r'wyniki imienne:\s*(.*)', sec, re.S | re.I)
        if im:
            seg = im.group(1)
            # Usuń linie wodne "Wygenerowano..." oraz znaczniki czasu — pojawiają
            # się MIĘDZY blokami/wotami i nie wolno na nich ucinać (patrz pułapka).
            seg = re.sub(r'(?im)^\s*Wygenerowano[^\n]*\n?', '', seg)
            seg = re.sub(r'(?m)^\s*\d{4}-\d{2}-\d{2}\s*(\d{2}:\d{2}:\d{2})?\s*\n?', '', seg)
            # Usuń podsumowanie frekwencji "Uczestnictwo w głosowaniach..." (oba formaty)
            seg = re.split(r'\n\s*Uczestnictwo', seg, maxsplit=1)[0]
            # Posprzątaj ewentualne "Przygotował:" (podpis podsumowania)
            seg = re.sub(r'(?im)^\s*Przygotował:.*$', '', seg)
            if re.search(r'^\s*ZA\s*\(\s*\d+\s*\)', seg):
                # Format B — bloki
                body = re.sub(r'wyniki imienne:?', '', seg, flags=re.I)
                headers = list(re.finditer(
                    r'^(?:ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\(\s*\d+\s*\)\s*$',
                    body, re.M))
                for hidx, h in enumerate(headers):
                    label = h.group(0).split('(')[0].strip()
                    st = h.end()
                    en = headers[hidx + 1].start() if hidx + 1 < len(headers) else len(body)
                    chunk = re.sub(r'Wygenerowano.*', '', body[st:en], flags=re.S)
                    names = [_norm_name(x) for x in chunk.split(',')
                             if _norm_name(x) and not _norm_name(x).startswith('Wygenerowano')]
                    key = _VOTE_MAP.get(label)
                    if key:
                        {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz, 'brak_glosu': bg, 'nieobecni': nb}[key].extend(names)
            else:
                # Format A — inline "Name (VOTE)"; ucięcie tylko podsumowania
                # "Uczestnictwo w głosowaniach" na końcu listy imiennej.
                segA = re.split(r'\n\s*Uczestnictwo', seg, maxsplit=1)[0]
                marker = (r'(?:ZA|PRZECIW|WSTRZYMUJĘ\s+SIĘ|WSTRZYMUJE\s+SIĘ|'
                          r'BRAK\s+GŁOSU|NIEOBECNI)')
                for m in re.finditer(r'([^,]+?)\s*\((' + marker + r')\)', segA, re.S):
                    nm = _norm_name(m.group(1))
                    key = _VOTE_MAP.get(" ".join(m.group(2).split()))
                    if nm and key:
                        {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz, 'brak_glosu': bg, 'nieobecni': nb}[key].append(nm)
        votes.append({'topic': topic, 'za': za, 'przeciw': pr, 'wstrzymal_sie': wz,
                      'brak_glosu': bg, 'nieobecni': nb})
    return votes


def parse_report_pdf(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full = '\n'.join((p.extract_text() or "") for p in pdf.pages)
    date = _parse_session_date(full)
    votes = _parse_votes(full)
    return date, votes


# ---------------------------------------------------------------------------
# 3. Budowanie outputu (identycznie jak inne miasta Radoskopa)
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name: str) -> str:
    return CLUB_BY_DISPLAY_NORM.get(_norm(name), "NZ")


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
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
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
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

    print("=== Scraper Rada Miejska Police (bip.police.pl) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji (z raportem głosowań): {len(sessions)}")

    records = []
    ok = fail = 0
    total_votes = 0
    for s in sessions:
        try:
            data = fetch(f"{BIP}/attachments/download/{s['report_id']}", cache_dir, binary=True)
            date, votes = parse_report_pdf(data)
            if not date or date < KAD_START:
                continue
            for v in votes:
                records.append({"session_date": date, "session_num": "",
                                "topic": v["topic"], "named": {
                                    "za": v["za"], "przeciw": v["przeciw"],
                                    "wstrzymal_sie": v["wstrzymal_sie"],
                                    "brak_glosu": v["brak_glosu"], "nieobecni": v["nieobecni"]}})
                total_votes += 1
            ok += 1
        except Exception as e:
            print(f"    BŁĄD {s['report_id']}: {e}")
            fail += 1

    print(f"  Raporty OK: {ok}, błędy: {fail}, głosowań (IX kad): {total_votes}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)

    kad = output["kadencje"][0]
    print(f"  SESJE: {kad['total_sessions']}, GŁOSOWANIA: {kad['total_votes']}, "
          f"RADNYCH: {kad['total_councilors']}")
    print(f"  KLUBY: {kad['clubs']}")
    print("  OK — zapisano data.json / kadencja-2024-2029.json / profiles.json")


if __name__ == "__main__":
    main()
