#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Nysa — imienne głosowania Rady Miejskiej w Nysie.

Źródło: BIP Urzędu Miejskiego w Nysie (https://nysa.bip.net.pl, legacy
Sputnik Software bip.net.pl platform, parametry ?c=kategoria / ?a=artykuł /
?p=document&action=save&id={doc}&bar_id={art}).

Rada Miejska w Nysie (IX kadencja 2024–2029) publikuje per sesję, w kategorii
"Protokoły Sesji" (per rok), artykuł protokołu z załączonym tekstowym PDF-em
"Wyniki głosowań / Wykaz głosowań / Imienny wykaz głosowań z NNN sesji Rady
Miejskiej w Nysie w dniu DD miesiąc RRRR" — raport imienny w formacie eSesja
standard. Występują DWA warianty sekcji "Wyniki imienne:":
  * blokowy:   "ZA (17)\n<nazwiska przecinkiem>\nPRZECIW (0)\n..."
  * inline:    "Sebastian Bem (ZA), Łukasz Bogdanowski (ZA), ..."
Parser obsługuje oba (wykrywa tryb i parsuje pozycyjnie).

Zakres: wszystkie sesje IX kadencji (I..XXXIII, 2024-05-07 .. 2026-07-27)
widoczne w Protokołach Sesji 2024/2025/2026.

Użycie:
    python scrape_nysa.py --output docs/data.json --profiles docs/profiles.json
                          [--config cities/nysa/config.json] [--cache-dir ...]
"""
import argparse
import difflib
import io
import json
import os
import re
import subprocess
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_voting_pdf_table import parse_polish_date  # noqa: E402

BASE = "https://nysa.bip.net.pl"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
IX_START = "2024-05-07"  # I sesja IX kadencji

# Protokoły Sesji II: kategorie per rok (kadencja IX: 2024-05-07 => 2024,2025,2026)
YEAR_CATS = {"2024": "509", "2025": "541", "2026": "597"}

UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0)", "Accept-Language": "pl-PL,pl;q=0.9"}

CLUB_ASSIGN = None  # z config.json

_VOTE_HEADER_RE = re.compile(r"(?:Wyniki|Wykaz|Imienny wykaz)\s+g[łl]osowa[ńn]", re.I)

_ESESJA_FOOTER = re.compile(
    r"Wygenerowano za pomo[cć][aą] app\.esesja\.pl\s*(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})?")
_BARE_TS = re.compile(r"\n\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\n")

_CAT_MAP = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu",
    "NIE GŁOSOWAŁ": "brak_glosu",
    "NIEOBECNI": "nieobecni",
}
_NAMED_KEYS = ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]
_CAT_PAT = r"ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMAŁ SIĘ|WSTRZYMUJE SIĘ|BRAK GŁOSU|NIE GŁOSOWAŁ|NIEOBECNI"
_NAME_BLACKLIST = {"przygotowała", "przygotowala", "przygotowała:", "wygenerowano",
                   "izabela", "tyczyńska", "tyczynska", "strona", "obecni:", "nieobecni:"}

_COUNTS_RE = re.compile(
    r"ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYM[^:]+?:\s*(\d+),"
    r"\s*BRAK G[ŁL]OSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)", re.I)


def _clean_name(nm):
    nm = re.sub(r"\s+", " ", nm).strip()
    nm = re.sub(r"\s*-\s*", "-", nm)          # "Trytko- Warczak" -> "Trytko-Warczak"
    nm = nm.strip(",;:.")
    return nm


def _norm(s):
    """Normalizacja do porównań fuzzy (NFKD, bez diakrytyków, tylko litery)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", "", s.lower())


def _valid_name(nm):
    if len(nm.split()) < 2:
        return False
    if len(nm) > 45 or len(nm) < 4:
        return False
    if not re.match(r"^[A-ZĄĆĘŁŃÓŚŹŻ]", nm):
        return False
    if re.search(r"\d", nm):
        return False
    low = nm.lower()
    if ":" in nm or any(b in low for b in _NAME_BLACKLIST):
        return False
    if re.search(r"[()\\/]", nm):
        return False
    return True


def _quote(s):
    from urllib.parse import quote
    return quote(s)


def _fetch(url, session, cache_dir):
    if cache_dir:
        fp = cache_dir / (_quote(url) + ".bin")
        if fp.exists() and fp.stat().st_size:
            return fp.read_bytes()
    for attempt in range(3):
        try:
            r = session.get(url, timeout=45)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            return data
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url)


# ---------------------------------------------------------------------------
# Parsowanie raportu imiennego (obsługuje 2 warianty "Wyniki imienne:")
# ---------------------------------------------------------------------------
def parse_report_bytes(data):
    full = ""
    first = ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, p in enumerate(pdf.pages):
            t = p.extract_text() or ""
            if i == 0:
                first = t
            full += t + "\n"
    full = _ESESJA_FOOTER.sub("", full)
    full = _BARE_TS.sub("\n", full)
    full = re.sub(r"(?m)^\s*Przygotowa[łl][aą]:.*$", "", full)
    full = re.sub(r"(?m)^\s*Osoby nieobecne w trakcie g[łl]osowania.*$", "", full)
    return full, first


def _pdf_char_count(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return sum(len((p.extract_text() or "").strip()) for p in pdf.pages)


def ocr_report_bytes(data):
    """OCR skanowanego raportu (brak warstwy tekstowej): pdfplumber->png->tesseract -l pol."""
    full = ""
    first = ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, p in enumerate(pdf.pages):
            png = f"/tmp/nysa_ocr_{os.getpid()}_{i}.png"
            p.to_image(resolution=200).save(png)
            try:
                r = subprocess.run(["tesseract", png, "-", "-l", "pol"],
                                   capture_output=True, text=True, timeout=120)
                t = r.stdout or ""
            finally:
                if os.path.exists(png):
                    os.remove(png)
            if i == 0:
                first = t
            full += t + "\n"
    full = _ESESJA_FOOTER.sub("", full)
    full = re.sub(r"(?m)^\s*Przygotowa[łl][aą]:.*$", "", full)
    return full, first


_INLINE_RE = None


def _inline_matches(seg):
    return re.findall(rf"([A-ZĄĆĘŁŃÓŚŹŻ][^()]*?)\s*\(\s*({_CAT_PAT})\s*\)", seg)


def _add_segment(named, seg, default_key):
    """Dodaj nazwiska z segmentu: inline 'Name (CAT)' ma priorytet, inaczej do default_key."""
    seg = re.sub(r"\s*\d+\s*\.\s*$", "", seg)
    # Odetnij nagłówek następnego głosowania ("{N}. <tekst>"), który wycieka
    # na koniec bloku — nazwiska nigdy nie zawierają "{N}. ".
    seg = re.split(r"\n\s*\d+\.[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ]", seg, maxsplit=1)[0] \
        if "\n" in seg else seg
    inline = _inline_matches(seg)
    if inline:
        for raw, cat in inline:
            name = _clean_name(raw)
            if _valid_name(name):
                named[_CAT_MAP.get(cat, default_key)].append(name)
        return
    names_text = re.sub(r"\s+", " ", seg)
    for nm in names_text.split(","):
        nm = _clean_name(nm)
        if _valid_name(nm):
            named[default_key].append(nm)


def parse_named_section(sec):
    named = {k: [] for k in _NAMED_KEYS}

    markers = list(re.finditer(rf"({_CAT_PAT})\s*\(\d+\)\s*", sec))
    if not markers:
        # czysty inline: "Imię Nazwisko (CAT), ..."
        for raw, cat in _inline_matches(sec):
            name = _clean_name(raw)
            if _valid_name(name):
                named[_CAT_MAP.get(cat, "brak_glosu")].append(name)
        return named

    # Nazwiska przed pierwszym markerem (brak nagłówka "ZA (N)") — dla ZA
    _add_segment(named, sec[:markers[0].start()], "za")
    for i, m in enumerate(markers):
        key = _CAT_MAP.get(m.group(1), "brak_glosu")
        seg_end = markers[i + 1].start() if i + 1 < len(markers) else len(sec)
        seg = sec[m.end():seg_end]
        if i == len(markers) - 1:
            seg = re.sub(r"\s*\d+\s*\.\s*$", "", seg)
        _add_segment(named, seg, key)
    return named


def parse_session_report(full_text, first_page, source_name=""):
    iso = parse_polish_date(first_page or full_text[:2000]) \
        or parse_polish_date(full_text)
    num = None
    m = re.search(r"z\s+([IVXLCDM]+)\s+sesji", first_page or "") \
        or re.search(r"\b([IVXLCDM]+)\s+[Ss]esj", first_page or full_text[:2000])
    if m:
        num = m.group(1)

    # Nysa pomija dwukropek po "Głosowano w sprawie" — dodajemy (split płytszy);
    # case-insensitive (OCR potrafi dać małą literę).
    full_text = re.sub(r"(?i)g[łl]osowano w sprawie(?!:)", "Głosowano w sprawie:", full_text)
    parts = re.split(r"(?i)g[łl]osowano(?: wniosek)? w sprawie:\s*", full_text)

    votes = []
    for block in parts[1:]:
        tm = re.search(r"(.*?)(?:Wyniki głosowania)", block, re.DOTALL | re.I)
        topic = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
        cm = _COUNTS_RE.search(block)
        counts = None
        if cm:
            counts = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
                      "wstrzymal_sie": int(cm.group(3)),
                      "brak_glosu": int(cm.group(4)), "nieobecni": int(cm.group(5))}
        named = {}
        nsm = re.search(r"Wyniki imienne:?\s*", block, re.I)
        if nsm:
            named = parse_named_section(block[nsm.end():])
        votes.append({"topic": topic, "counts": counts, "named_votes": named})
    return {"date": iso, "num": num, "votes": votes}


# ---------------------------------------------------------------------------
# 1. Lista artykułów protokołów w kategorii roku
# ---------------------------------------------------------------------------
def list_protocol_articles(year, session, cache_dir):
    from bs4 import BeautifulSoup
    soup_txt = _fetch(BASE + f"?c={YEAR_CATS[year]}", session, cache_dir)
    soup = BeautifulSoup(soup_txt.decode("utf-8", errors="replace"), "lxml")
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.match(r"\?a=(\d+)\s*$", a["href"])
        t = a.get_text(" ", strip=True)
        if m and m.group(1) not in seen and t and t != "Czytaj dalej":
            seen.add(m.group(1))
            out.append({"aid": m.group(1), "title": t,
                        "url": BASE + f"?a={m.group(1)}"})
    return out


def get_attachment_ids(aid, session, cache_dir):
    txt = _fetch(BASE + f"?a={aid}", session, cache_dir).decode("utf-8", errors="replace")
    txt = txt.replace("&amp;", "&")
    ids = []
    for m in re.finditer(r"\?p=document&action=save&id=(\d+)", txt):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def find_vote_report_text(aid, doc_ids, session, cache_dir):
    """Zwraca (full_text, first_page) raportu imiennego głosowań (tekstowy lub OCR)."""
    ocr_candidates = []
    for did in doc_ids:
        data = _fetch(f"{BASE}?p=document&action=save&id={did}&bar_id={aid}",
                      session, cache_dir)
        if data[:4] != b"%PDF":
            continue
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = pdf.pages
                first = (pages[0].extract_text() or "") if pages else ""
                two = "\n".join((p.extract_text() or "") for p in pages[:2])
                chars = sum(len((p.extract_text() or "").strip()) for p in pages)
        except Exception:
            continue
        if _VOTE_HEADER_RE.search(first[:250]) and "Wyniki imienne" in two:
            return parse_report_bytes(data)
        if chars < 50:
            ocr_candidates.append(data)
    # Brak tekstowego raportu — próbuj OCR skanów. Sygnatura raportu głosowań:
    # "imienne" ORAZ bloki "głosowano w sprawie" (narracyjny protokół ich nie ma).
    for data in ocr_candidates:
        full, first = ocr_report_bytes(data)
        if "imienne" in full.lower() and re.search(r"(?i)g[łl]osowano\s+w sprawie", full):
            return full, first
    return None


# ---------------------------------------------------------------------------
# Budowa outputu (wzorzec jastrzebie / swidnica)
# ---------------------------------------------------------------------------
def build_output(votes):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in votes:
        d = rec["date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named_votes"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named_votes"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
            "topic": rec.get("topic") or "",
            "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in
                       ("za", "przeciw", "wstrzymal_sie")},
            "source_url": rec.get("source_url", ""),
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"],
                              "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]),
                              "speakers": []})

    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)

    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": _club_of(name),
                                 "district": None, "votes_za": 0, "votes_przeciw": 0,
                                 "votes_wstrzymal": 0, "votes_brak": 0,
                                 "votes_nieobecny": 0, "votes_with_club": 0,
                                 "votes_against_club": 0, "rebellions": []}
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
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) \
            if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekwencja, 1),
                                "aktywnosc": round(aktywnosc, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"],
                                "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
                                "votes_total": total_votes, "rebellion_count": 0,
                                "rebellions": [], "has_activity_data": False, "activity": None})

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
        same = sum(1 for vv in common if vectors[a][vv] == vectors[b][vv])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1),
                      "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(),
            "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def _club_of(name):
    if CLUB_ASSIGN:
        return CLUB_ASSIGN.get(name, "NZ")
    return ""


def _slug(name):
    import unicodedata
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def build_profiles(output):
    profs = []
    for kad in output.get("kadencje", []):
        for c in kad.get("councilors", []):
            profs.append({"name": c["name"], "slug": _slug(c["name"]),
                          "kadencje": {KADENCJA_ID: {
                              "club": c["club"], "has_voting_data": True,
                              "has_activity_data": False, "frekwencja": c["frekwencja"],
                              "aktywnosc": c["aktywnosc"],
                              "zgodnosc_z_klubem": c["zgodnosc_z_klubem"],
                              "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                              "votes_wstrzymal": c["votes_wstrzymal"],
                              "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
                              "votes_total": c["votes_total"], "rebellion_count": 0,
                              "rebellions": [], "roles": [], "notes": "",
                              "former": False, "mid_term": False}}})
    return {"profiles": profs}


def save_split(output, out_path):
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


# ---------------------------------------------------------------------------
# Rezolucja nazwisk do kanonicznego rosteru
# ---------------------------------------------------------------------------
def _resolve_to_roster(votes):
    """Zwraca (votes, roster). Roster = nazwiska występujące w >=3 sesjach.
    Nazwiska-sklejki (utracony przecinek na złamaniu wiersza) rozbijamy na
    członków rosteru; artefakty (nazwy komisji, stopki) odrzucamy."""
    sess_per_name = defaultdict(set)
    for v in votes:
        for names in v["named_votes"].values():
            for n in names:
                sess_per_name[n].add(v["date"])
    roster = sorted(n for n, ss in sess_per_name.items() if len(ss) >= 3)
    roster_sorted = sorted(roster, key=len, reverse=True)
    roster_set = set(roster)
    roster_norm = {_norm(n): n for n in roster}

    def fuzzy(name):
        key = _norm(name)
        best, br = None, 0.0
        for m in roster_sorted:
            r = difflib.SequenceMatcher(None, key, _norm(m)).ratio()
            if r > br:
                br, best = r, m
        return best if br >= 0.82 else None

    def resolve(name):
        if name in roster_set:
            return [name]
        toks = []
        rest = name.strip()
        while rest:
            hit = None
            for m in roster_sorted:
                if rest == m:
                    hit = m
                    rest = ""
                    break
                if rest.startswith(m + " "):
                    hit = m
                    rest = rest[len(m):].strip()
                    break
            if hit is None:
                return []
            toks.append(hit)
        if toks:
            return toks
        # pojedyncze nazwisko z potencjalnym błędem OCR
        m = fuzzy(name)
        return [m] if m else []

    out = []
    for v in votes:
        nv = {}
        for cat, names in v["named_votes"].items():
            res = []
            for n in names:
                res.extend(resolve(n))
            nv[cat] = res
        v2 = dict(v)
        v2["named_votes"] = nv
        out.append(v2)
    return out, roster


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global CLUB_ASSIGN
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        CLUB_ASSIGN = cfg.get("club_assignments") or None

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    session = requests.Session()
    session.headers.update(UA)

    sessions = []
    n_mismatch = 0
    for year in ("2024", "2025", "2026"):
        arts = list_protocol_articles(year, session, cache_dir)
        print(f"[nysa] {year}: {len(arts)} artykułów protokołów")
        for art in arts:
            doc_ids = get_attachment_ids(art["aid"], session, cache_dir)
            got = find_vote_report_text(art["aid"], doc_ids, session, cache_dir) \
                if doc_ids else None
            if got is None:
                print(f"    BRK raportu imiennego: ?a={art['aid']} {art['title'][:45]}")
                continue
            full_text, first_page = got
            parsed = parse_session_report(full_text, first_page, art["title"])
            if not parsed.get("votes"):
                print(f"    BRK głosowań: ?a={art['aid']}")
                continue
            date = parsed.get("date")
            if not date or date < IX_START:
                if date:
                    print(f"    pominięto (pre-IX {date}): {art['title'][:45]}")
                continue
            num = parsed.get("num")
            if not num:
                m = re.search(r"\b([IVXLCDM]+)\s+sesj", art["title"])
                if m:
                    num = m.group(1)
            sessions.append({"date": date, "num": num or "",
                             "votes": parsed["votes"],
                             "source_url": art["url"]})
            print(f"    sesja {num or '?'} {date}: {len(parsed['votes'])} głosowań")
            if args.limit and len(sessions) >= args.limit:
                break
        if args.limit and len(sessions) >= args.limit:
            break

    if not sessions:
        print("[nysa] BRAK DANYCH — nic nie zapisano")
        sys.exit(1)

    votes = []
    for s in sessions:
        for v in s["votes"]:
            nv = {k: list(vals) for k, vals in (v.get("named_votes") or {}).items()}
            if not nv:
                n_mismatch += 1
                continue
            votes.append({"date": s["date"], "num": s["num"],
                          "topic": v.get("topic") or "",
                          "named_votes": nv, "source_url": s["source_url"],
                          "counts": v.get("counts")})

    # Rezolucja nazwisk do kanonicznego rosteru (scala split nazwisk na złamaniu
    # wiersza np. "Adam Zelent Maria Żukowska-Jacykowska" -> 2 radnych; odrzuca
    # artefakty typu nazwa komisji / stopka).
    votes, ROSTER = _resolve_to_roster(votes)

    # WALIDACJA: publikujemy tylko głosowania, których sumy imienne dokładnie
    # == agregatowi "ZA: N, PRZECIW: ...". Głosowania z rozjazdem (formaty
    # korekt, OCR błędy, artefakty źródła) są ODRZUCANE — lepiej mniej danych,
    # niż dane z błędnym przypisaniem per-radny.
    kept = []
    n_dropped = 0
    for v in votes:
        c = v.get("counts")
        if not c:
            kept.append(v)
            continue
        nv = v["named_votes"]
        ok = all(c.get(agg) is None or len(nv.get(agg, [])) == c[agg]
                 for agg in ("za", "przeciw", "wstrzymal_sie"))
        if ok:
            kept.append(v)
        else:
            n_dropped += 1
    if n_dropped:
        print(f"[nysa] odrzucono {n_dropped} głosowań (rozjazd z agregatem)")

    output = build_output(kept)
    save_split(output, args.output)
    if args.profiles:
        profs = build_profiles(output)
        with open(args.profiles, "w", encoding="utf-8") as f:
            json.dump(profs, f, ensure_ascii=False, separators=(",", ":"))
        print(f"[nysa] profiles.json: {len(profs['profiles'])} profili")

    k = output["kadencje"][0]
    print(f"\n[nysa] ZAPISANO: {k['total_sessions']} sesji, {k['total_votes']} głosowań, "
          f"{k['total_councilors']} radnych (odrzucone={n_dropped})")


if __name__ == "__main__":
    main()
