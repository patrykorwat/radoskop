#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Świnoujście — imienne głosowania Rady Miasta Świnoujście (IX kadencja).

Źródło: BIP bip.um.swinoujscie.pl (CMS eBOI-подобny 'artykuly'), kategoria
"Sesje Rady Miasta IX Kadencja" (/artykuly/1865/sesje-rady-miasta-ix-kadencja,
paginacja /artykuly/1865/{page}/10/...). Dla każdej sesji artykuł per-sesja ma
sekcję "Załączniki" z załącznikiem "Wyniki głosowania" (PDF,
https://bip.um.swinoujscie.pl/attachments/download/{id}) zawierającym IMIENNE
głosowania per radny — format wydruku eSesja: jedna strona = jedno głosowanie,
agregaty "Liczba uprawnionych/obecnych/nieobecnych" + "Głosy za/przeciw/
wstrzymujące się/Obecni niegłosujący" + dwukolumnowa tabela
"Lp | Nazwisko i imię | Głos" (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEOBECNY(A) /
OBECNY(A)). Walidacja per głos: sumy imienne == agregaty z nagłówka.

Użycie:
    python scrape_swinoujscie.py --city-dir <cities/swinoujscie> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
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
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.um.swinoujscie.pl"
CATEGORY = "/artykuly/1865/sesje-rady-miasta-ix-kadencja"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10,
    "października": 10, "listopada": 11, "grudnia": 12,
}
_ROM = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
        "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
        "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
        "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
        "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32,
        "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36, "XXXVII": 37,
        "XXXVIII": 38, "XXXIX": 39, "XL": 40}


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _norm_vote(w):
    k = _nk(w)
    if k in ("za", "z", "ża", "ze", "tak"):
        return "za"
    if k.startswith("przeciw") or k == "nie":
        return "przeciw"
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("nieobecn"):
        return "nieobecni"
    if k.startswith("obecn"):
        return "obecny"
    return None


REQ_DELAY = 0.5
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        (cache_dir / (key + ".dat")).write_bytes(data)
    return data


# ---------------- discovery ----------------
def discover_sessions():
    """Paginate the IX-kadencja sessions category; return [{url,title,date,num}]."""
    from html import unescape
    sessions = {}
    page = 1
    while page <= 8:
        url = f"{BIP}/artykuly/1865/{page}/25/sesje-rady-miasta-ix-kadencja"
        try:
            t = _get(url, None).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  [warn] {url}: {e}")
            break
        new = 0
        for m in re.finditer(r'href="([^"]*?/artykul/1865/\d+/[^"]+)"[^>]*>\s*([^<]{10,200})', t):
            href_full = m.group(1)
            href = href_full[href_full.find("/artykul/1865/"):]
            title = " ".join(unescape(m.group(2)).split())
            if "sesja Rady Miasta" not in title:
                continue
            dm = re.search(r"w dniu (\d{1,2}) (stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|pazdziernika|października|listopada|grudnia)", title)
            if not dm:
                continue
            mon = _MONTHS.get(dm.group(2))
            yr = re.search(r"(\d{4})\s*r\.", title)
            year = int(yr.group(1)) if yr else 2024
            date = f"{year}-{mon:02d}-{int(dm.group(1)):02d}"
            if date < KAD_START:
                continue
            rm = re.match(r"^(XXX[IVX]*|XX[IVX]*|X[IVX]*|IX|VIII|VII|VI|V|IV|III|II|I)\b", title)
            num = str(_ROM.get(rm.group(1), "")) if rm else ""
            if href not in sessions:
                sessions[href] = {"url": BIP + href, "title": title, "date": date, "num": num}
                new += 1
        if new == 0:
            break
        page += 1
        time.sleep(0.2)
    out = sorted(sessions.values(), key=lambda s: s["date"])
    return out


def find_results_pdf(article_html):
    """Return attachment download URL of the 'Wyniki głosowania' attachment (or None)."""
    for m in re.finditer(r'href="(https://bip\.um\.swinoujscie\.pl/attachments/download/\d+)"[^>]*>\s*([^<]{3,200})', article_html):
        name = " ".join(m.group(2).split())
        if _nk(name).startswith("wynikiglosowania"):
            return m.group(1)
    return None


# ---------------- PDF parsing (identyczny layout eSesja-print co goleniow) ----
def _table_region(words):
    up = [w for w in words if _nk(w["text"]) == "uprawnieni"]
    if not up:
        return words, 0
    thr = max(w["top"] for w in up)
    return [w for w in words if w["top"] > thr + 4], thr


def _col_boundary(words):
    right_lps = [w["x0"] for w in words if re.match(r"^\d{1,2}\.$", w["text"]) and w["x0"] > 150]
    if right_lps:
        return min(right_lps) - 3
    lps = [w["x0"] for w in words if re.match(r"^\d{1,2}\.$", w["text"])]
    return (min(lps) + max(lps)) / 2.0 if lps else 297.0


def _parse_column(col):
    col.sort(key=lambda t: (t[0], t[1]))
    rows = []
    cr = None
    for top, x0, t in col:
        if cr is None or top - cr[0] > 6:
            cr = [top, []]
            rows.append(cr)
        cr[1].append((x0, t))
    out = []
    cur = None

    def emit():
        nonlocal cur
        if cur is not None and cur.get("vote"):
            out.append((cur["name"].strip(), cur["vote"]))
        cur = None

    for top, toks in rows:
        toks.sort(key=lambda z: z[0])
        if re.match(r"^\d+\.$", toks[0][1]):
            emit()
            cur = {"name": "", "vote": None}
            toks = toks[1:]
        elif cur is None:
            cur = {"name": "", "vote": None}
        for _x, t in toks:
            nv = _norm_vote(t)
            if nv in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "obecny"):
                cur["vote"] = nv
            elif _nk(t) in ("sie", "sier"):
                pass
            elif re.match(r"(?i)^(wydrukowano:?|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}:\d{2}(:\d{2})?)$", t):
                pass
            else:
                cur["name"] = (cur["name"] + " " + t).strip()
    emit()
    return out


def _table_cells(words):
    b = _col_boundary(words)
    L = [(w["top"], w["x0"], w["text"]) for w in words if w["x0"] < b]
    R = [(w["top"], w["x0"], w["text"]) for w in words if w["x0"] >= b]
    cells = []
    for col in (L, R):
        cells += _parse_column(col)
    return cells


def _extract_aggs(text):
    agg = {}
    for key, pat in [("uprawnionych", r"Liczba uprawnionych\s+(\d+)"),
                     ("obecnych", r"Liczba obecnych\s+(\d+)"),
                     ("nieobecnych", r"Liczba nieobecnych\s+(\d+)"),
                     ("obecni_nieglosujacy", r"Obecni niegłosujący\s+(\d+)"),
                     ("za", r"Głosy za\s+(\d+)"),
                     ("przeciw", r"Głosy przeciw\s+(\d+)"),
                     ("wstrzym", r"Głosy wstrzymujące się\s+(\d+)")]:
        m = re.search(pat, text)
        if m:
            agg[key] = int(m.group(1))
    return agg


def _extract_topic(text):
    m = re.search(r"Typ głosowania", text)
    pre = text[:m.start()] if m else text
    pre = re.sub(r"(?is)^.*?sesja Rady Miasta Świnoujście[^\n]*\n", "", pre)
    pre = pre.replace("Głosowanie", " ")
    lines = [l.strip() for l in pre.split("\n")]
    out = []
    for l in lines:
        if not l:
            continue
        if re.match(r"^(Nr\s*)?\d+(\.|,)?$", l):
            continue
        out.append(l)
    topic = " ".join(out)
    topic = re.sub(r"\s+", " ", topic).strip(" .:,;-")
    return topic or "(glosowanie)"


def _dynamic_roster(votes):
    """Roster derived from the PDFs themselves (surname-first source names)."""
    names = set()
    for v in votes:
        for nm, _vote in v["cells"]:
            if nm and nm != "?":
                names.add(nm)
    return sorted(names)


def _display(nm):
    toks = [_normalize_case(t) for t in nm.split()]
    return " ".join(reversed(toks)) if len(toks) == 2 else " ".join(toks)


_CASE_RE = re.compile(r"^[A-ZŚŁŻŹĆŃÓ][\wŚŁŻŹĆŃÓąęśłżźćńó'-]*$")


def _normalize_case(tok):
    """ANTCZAK -> Antczak; Jan -> Jan; leaves hyphenated/Mac forms intact."""
    if tok.isupper() and len(tok) > 2 and _CASE_RE.match(tok):
        return tok[0] + tok[1:].lower()
    return tok


def _extract_aggs_b(text):
    """Aggregates for the older TAK/NIE print (variant B)."""
    agg = {}
    for key, pat in [("za", r"TAK\s+(\d+)"),
                     ("przeciw", r"NIE\s+(\d+)"),
                     ("wstrzym", r"WSTRZYMAŁO SIĘ\s+(\d+)"),
                     ("glosowalo", r"GŁOSOWAŁO\s+(\d+)\s+z\s+(\d+)")]:
        m = re.search(pat, text)
        if m:
            agg[key] = [int(g) for g in m.groups()] if key == "glosowalo" else int(m.group(1))
    return agg


def _parse_column_b(words):
    """Single-column 'L.p. Nazwisko i imię Głos' table (variant B)."""
    rows_w = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    cr = None
    for w in rows_w:
        if cr is None or w["top"] - cr[0] > 6:
            cr = [w["top"], []]
            rows.append(cr)
        cr[1].append((w["x0"], w["text"]))
    out = []
    cur = None

    def emit():
        nonlocal cur
        if cur is not None and cur.get("vote"):
            out.append((cur["name"].strip(), cur["vote"]))
        cur = None

    for _top, toks in rows:
        toks.sort(key=lambda z: z[0])
        if re.match(r"^\d{1,2}\.?$", toks[0][1]) and cur is None or (toks and re.match(r"^\d{1,2}\.?$", toks[0][1])):
            emit()
            cur = {"name": "", "vote": None}
            toks = toks[1:]
        elif cur is None:
            continue
        for _x, t in toks:
            nv = _norm_vote(t)
            if nv in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "obecny"):
                cur["vote"] = nv
            elif _nk(t) in ("sie", "si", "glosowanie"):
                pass
            elif re.match(r"(?i)^(\d{1,2}\.\d{1,2}\.\d{4}|data)$", t):
                pass
            elif re.match(r"^\d{1,2}:\d{2}$", t):
                pass
            else:
                cur["name"] = (cur["name"] + " " + t).strip()
    emit()
    # keep only plausible 2-3 word names
    out = [(nm, v) for nm, v in out if 1 < len(nm.split()) <= 4]
    return out


def _parse_pdf_variant_b(pdf):
    """Older eSesja print: one vote per page, 'Głosowanie Nr N' + TAK/NIE aggregates +
    single-column L.p table. Validate per-vote named sums vs aggregates."""
    records = []
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Głosowanie Nr" not in text:
            continue
        agg = _extract_aggs_b(text)
        # topic: lines between 'Głosowanie Nr N' and the TAK line
        m = re.search(r"Głosowanie Nr\s*\d+\s*\n(.*?)\nTAK\s+\d+", text, re.S)
        topic = re.sub(r"\s+", " ", m.group(1)).strip(" .:,;-") if m else "(glosowanie)"
        # table region: words below the KWORUM / GŁOSOWAŁO line
        words = page.extract_words()
        anchor = [w for w in words if _nk(w["text"]) in ("kworum", "glosowalo")]
        if anchor:
            thr = max(w["bottom"] for w in anchor)
            tw = [w for w in words if w["top"] > thr]
        else:
            tw = words
        cells = _parse_column_b(tw)
        counter = Counter(vote for _n, vote in cells)
        gl = agg.get("glosowalo", [])
        voted = counter.get("za", 0) + counter.get("przeciw", 0) + counter.get("wstrzymal_sie", 0)
        ok = (counter.get("za", 0) == agg.get("za", -1)
              and counter.get("przeciw", 0) == agg.get("przeciw", -1)
              and counter.get("wstrzymal_sie", 0) == agg.get("wstrzym", -1)
              and (not gl or voted == gl[0]))
        if not ok:
            continue
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for nm, vote in cells:
            if vote in named:
                named[vote].append(_display(nm))
        records.append({"topic": topic, "named": named})
    return records


def _parse_pdf_variant_c(pdf):
    """'Lista głosowania imiennego' single vote per PDF, X marks under Za/Przeciw/
    Wstrzymuję się columns. Positional column assignment via word x-coordinates."""
    page = pdf.pages[0]
    text = page.extract_text() or ""
    m = re.search(r"w sprawie (.*?)(?:\nLp\.|$)", text, re.S)
    topic = ("w sprawie " + re.sub(r"\s+", " ", m.group(1)).strip(" .:")) if m else "(glosowanie)"
    words = page.extract_words()
    heads = {}
    header_top = None
    for w in words:
        t = w["text"]
        if t == "Za" and "za" not in heads:
            heads["za"] = w["x0"]; header_top = w["top"]
        elif t == "Przeciw" and "przeciw" not in heads:
            heads["przeciw"] = w["x0"]
        elif t.startswith("Wstrzymuj"):
            heads["wstrzymal_sie"] = w["x0"]
    if "za" not in heads or "przeciw" not in heads or header_top is None:
        return []
    # boundaries: midpoint between header x positions
    b1 = (heads["za"] + heads["przeciw"]) / 2
    b2 = (heads["przeciw"] + heads.get("wstrzymal_sie", heads["przeciw"] + 120)) / 2
    # table region only (below headers)
    tw = [w for w in words if w["top"] > header_top - 2]
    # rows: group by Lp number lines (name may wrap)
    lp_words = sorted([w for w in tw if re.match(r"^\d{1,2}\.$", w["text"])], key=lambda w: w["top"])
    marks = [w for w in tw if w["text"] == "X"]
    # names: capitalized tokens to the left of b1 minus some margin, above each mark cluster
    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
    rows = []
    for i, lp in enumerate(lp_words):
        top_lo = lp["top"] - 3
        top_hi = lp_words[i + 1]["top"] - 3 if i + 1 < len(lp_words) else max(w["bottom"] for w in words) + 5
        name_ws = sorted([w for w in words if top_lo <= w["top"] < top_hi
                          and w["x0"] < heads["za"] - 10
                          and not (w["x0"] >= b1 and w["x0"] <= b2 + 60 and w["text"] in ("X", "--", "-"))],
                         key=lambda w: (w["top"], w["x0"]))
        name = " ".join(w["text"] for w in name_ws if re.match(r"^[A-ZŚŁŻŹĆŃÓ]", w["text"]) and w["text"] != "X")
        row_marks = [w for w in marks if top_lo <= w["top"] < top_hi]
        rows.append((name, row_marks))
    for name, row_marks in rows:
        if not name:
            continue
        d = _display(name)
        for w in row_marks:
            if w["x0"] < b1:
                named["za"].append(d)
            elif w["x0"] < b2:
                named["przeciw"].append(d)
            else:
                named["wstrzymal_sie"].append(d)
    total = sum(len(v) for v in named.values())
    if total < 5:
        return []
    return [{"topic": topic, "named": named}]


def parse_pdf_payload(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        # variant detection: newer per-page eSesja print (A) vs older TAK/NIE print (B)
        first_text = pdf.pages[0].extract_text() or ""
        if "Lista głosowania imiennego" in first_text:
            return _parse_pdf_variant_c(pdf)
        variant_b = ("GŁOSOWAŁO" in first_text or re.search(r"\bTAK\s+\d+", first_text)) and "Liczba uprawnionych" not in first_text
        if variant_b:
            return _parse_pdf_variant_b(pdf)
        votes = []
        cur = None
        for page in pdf.pages:
            words = page.extract_words()
            text = page.extract_text() or ""
            has_agg = "Liczba uprawnionych" in text
            tw, _ = _table_region(words)
            cells = _table_cells(tw)
            if has_agg:
                agg = _extract_aggs(text)
                topic = _extract_topic(text)
                cur = {"topic": topic, "agg": agg, "cells": cells}
                votes.append(cur)
            elif cur is not None:
                cur["cells"] = cur["cells"] + cells
    records = []
    for v in votes:
        counter = Counter(vote for _n, vote in v["cells"])
        ok = (counter.get("za", 0) == v["agg"].get("za", -1)
              and counter.get("przeciw", 0) == v["agg"].get("przeciw", -1)
              and counter.get("wstrzymal_sie", 0) == v["agg"].get("wstrzym", -1)
              and counter.get("nieobecni", 0) == v["agg"].get("nieobecnych", -1)
              and counter.get("obecny", 0) == v["agg"].get("obecni_nieglosujacy", -1))
        if not ok:
            continue
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for nm, vote in v["cells"]:
            if vote in named:
                named[vote].append(_display(nm))
        records.append({"topic": v["topic"], "named": named})
    return records


# ---------------- output (wzorzec goleniow) ----------------
def make_slug(name):
    repl = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z",
            "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z"}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0, "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    from itertools import combinations
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid2 in common if vectors[a][vid2] == vectors[b][vid2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / total * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions()
    print(f"[swinoujscie] {len(sessions)} sesji IX kad.")
    records = []
    for se in sessions:
        try:
            art = _get(se["url"], cache).decode("utf-8", "ignore")
            pdf_url = find_results_pdf(art)
            if not pdf_url:
                print(f"  [skip {se['date']} no 'Wyniki głosowania' attachment]")
                continue
            pdf_bytes = _get(pdf_url, cache)
            recs = parse_pdf_payload(pdf_bytes)
            for r in recs:
                r["date"] = se["date"]
                r["num"] = se["num"]
            records += recs
            print(f"  [ok] {se['date']} {se['num'] or '?':>4} votes={len(recs)}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")
    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    kad = output["kadencje"][0]
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    import shutil
    shutil.copy2(city_dir / "config.json", docs / "config.json")
    print(f"[swinoujscie] DONE votes={total_votes} sessions={total_sessions} councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
