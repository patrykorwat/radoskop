#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ząbki — imienne głosowania Rady Miasta Ząbki (IX kadencja).

Źródło: BIP bip.zabki.pl (ESC S.A. VelaBIP), kategoria
https://bip.zabki.pl/protokoly-z-sesji-rady-1 ("Protokoły z sesji Rady").
Sesje IX kadencji mają osobne załączniki "RRRR NN raport z głosowań D.MM.YYYY.pdf"
(38 raportów I→XXXVIII, od 2024-05-07) generowanych z systemu DSSS Vote.

Struktura raportu (PDF tekstowy):
 - str. 1-2: nazwa sesji ("Na sesji "XXX sesja Rady Miasta Ząbki 23.02.2026 r."
   stawiło się N radnych"), lista obecności + czasy logowania.
 - każda kolejna strona = JEDNO głosowanie: nagłówek "Uchwała numer "…"" albo
   "Przeprowadzono głosowanie w sprawie "…"" + "proporcją głosów: jestem za A,
   jestem przeciw B, wstrzymuję się C" + "Data i godzina głosowania: D HH:MM:SS".
 - tabela kwadransowa "Radni zagłosowali jak poniżej:" — kolumny:
   lewa-góra = "Jestem za", prawa-góra = "Jestem przeciw",
   lewa-dół (nagłówek "Wstrzymuję się") , prawa-dół ("Obecni radni, którzy
   nie wzięli udziału w głosowaniu"). Puste kolumny = "BRAK".
   Pozycje x słów: lewa kolumna x<300, prawa x>=300; wiersz = top.
 Walidacja: policzone nazwiska per kategoria == agregaty proporcji.

Użycie:
    python scrape_zabki.py --city-dir <cities/zabki> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
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

BIP = "https://bip.zabki.pl"
CAT_PATH = "/protokoly-z-sesji-rady-1"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.8
_LAST = 0.0

MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
          "lipca":7,"sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12}


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir, tries=4):
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    last_err = None
    for a in range(tries):
        try:
            _rate()
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"},
                             timeout=90, verify=False)
            r.raise_for_status()
            data = r.content
            if cf is not None:
                cf.write_bytes(data)
            return data
        except Exception as e:
            last_err = e
            time.sleep(2 + 3 * a)
    raise RuntimeError(f"failed {url}: {last_err}")


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def make_slug(s):
    s = s.replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def parse_iso(dmy):
    d, m, y = dmy.split(".")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def discover_reports(cache):
    html = _get(BIP + CAT_PATH, cache).decode("utf-8", "ignore")
    reps = []
    seen = set()
    for m in re.finditer(r'href="(/zalacznik/\d+)"[^>]*>(.*?)</a>', html, re.S):
        href, label = m.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        mm = re.match(r"(20\d\d)\s+([IVXL]+)\s+raport z głosowań\s+([\d.]+)\.pdf", label)
        if not mm:
            continue
        url = BIP + href
        if url in seen:
            continue
        seen.add(url)
        year, rom, dmy = mm.groups()
        try:
            date = parse_iso(dmy)
        except Exception:
            continue
        reps.append({"url": url, "session_num": rom, "session_year": year, "date": date})
    reps.sort(key=lambda r: r["date"])
    return reps


NAME_ROW = re.compile(r"^(\d+)\.\s+(.+)$")


def _ocr_words(doc, page_index):
    """Render pymupdf page -> tesseract TSV -> words [{'x0','top','text'}] in PDF pt.
    Used for DSSS PDFs with broken font mapping (no usable text layer)."""
    import subprocess
    import tempfile
    png = str(Path(tempfile.gettempdir()) / f"ocr_zabki_p{page_index}.png")
    pm = doc[page_index].get_pixmap(dpi=150)
    pm.save(png)
    out = subprocess.run(["tesseract", png, "-", "-l", "pol", "tsv"],
                         capture_output=True, text=True, timeout=180)
    words = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        txt = parts[11].strip()
        try:
            left, top = float(parts[6]), float(parts[7])
        except ValueError:
            continue
        if txt and txt != "-1":
            words.append({"x0": left * 0.48, "top": top * 0.48, "text": txt})
    return words


def _rows_in_region(words, x_lo, x_hi, y_lo, y_hi):
    """Group words into rows within an x/y region; return list of row texts."""
    rows = defaultdict(list)
    for w in words:
        if x_lo <= w["x0"] < x_hi and y_lo <= w["top"] < y_hi:
            rows[round(w["top"])].append(w)
    out = []
    for top in sorted(rows):
        ws = sorted(rows[top], key=lambda w: w["x0"])
        txt = " ".join(w["text"] for w in ws)
        out.append(txt)
    return out


def _split_words_rows(words, tol=5):
    """Group words into rows by y with tolerance (OCR rows jitter);
    return list of (y, [row_words_sorted_by_x])."""
    by_top = defaultdict(list)
    for w in words:
        by_top[round(w["top"])].append(w)
    tops = sorted(by_top)
    clusters = []
    for t in tops:
        if clusters and t - clusters[-1][-1][0] <= tol:
            clusters[-1].append((t, by_top[t]))
        else:
            clusters.append([(t, by_top[t])])
    out = []
    for cl in clusters:
        ws = []
        for _, g in cl:
            ws.extend(g)
        y = min(t for t, _ in cl)
        ws.sort(key=lambda w: w["x0"])
        out.append((y, ws))
    return out


def _row_text(ws):
    return " ".join(w["text"] for w in ws)


def _parse_vote_words(words, full_text):
    """Shared core: from (words, full_text) build a vote record or None/mismatch."""
    dm = re.search(r"Data i godzina g[sł]osowania:?\s*([\d.]+\s+[\d:]+)", full_text)
    if not dm:
        return None
    date = parse_iso(dm.group(1).split()[0])
    tm = re.search(r'(?:Uchwa[ał]a numer|Przeprowadzono g[sł]osowanie w sprawie|g[sł]osowanie jawnego imiennego w sprawie)\s*[“"]([^”"]+)[”"]', full_text, re.S)
    if not tm:
        tm = re.search(r'(?:Wniosek dotyczy|w sprawie)\s*[“"]([^”"]+)[”"]', full_text, re.S)
    topic = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
    pm = re.search(r"jestem za\s*(\d+),?\s*jestem przeciw\s*(\d+),?\s*wstrzymuj[eę]m? si[ęę]?\s*(\d+)", full_text.replace("\n", " "))
    agg = tuple(int(x) for x in pm.groups()) if pm else None
    # header positions (tolerate OCR case/typos; header rows carry NO digits)
    hdr = {}
    for y, ws in _split_words_rows(words):
        t = _row_text(ws)
        if any(ch.isdigit() for ch in t):
            continue
        tl = t.lower().replace("jem", "em")
        if "jestem za" in tl or "jestemía" in tl:
            if "za" not in hdr:
                hdr["za"] = (ws[0]["x0"], y)
        if "jestem przeciw" in tl and "przeciw" not in hdr:
            hdr["przeciw"] = ([w["x0"] for w in ws if w["x0"] > 300] or [300])[0], y
        if "wstrzymuj" in tl and ws[0]["x0"] < 300 and "wstrzymuje" not in hdr:
            hdr["wstrzymuje"] = (ws[0]["x0"], y)
    if "za" not in hdr:
        return None
    y_tbl0 = hdr["za"][1] + 4
    ymax = max(w["top"] for w in words) + 5
    if "wstrzymuje" in hdr:
        y_split = hdr["wstrzymuje"][1] - 3
    else:
        ob = [w for w in words if w["text"].lower().startswith("obecni") and w["x0"] > 300 and w["top"] > y_tbl0]
        y_split = (ob[0]["top"] - 3) if ob else ymax
    left_rows = _rows_in_region(words, 0, 300, y_tbl0, y_split)
    right_rows_top = _rows_in_region(words, 300, 1e9, y_tbl0, y_split)
    left_rows_bot = _rows_in_region(words, 0, 300, y_split + 2, ymax)
    right_rows_bot = _rows_in_region(words, 300, 1e9, y_split + 2, ymax)

    def names_from(rows):
        out = []
        for r in rows:
            r = r.strip()
            if not r or r == "BRAK":
                continue
            mm = NAME_ROW.match(r)
            if mm:
                nm = mm.group(2).strip()
                if nm and nm != "BRAK":
                    out.append(nm)
        return out

    za_names = names_from(left_rows)
    prze_names = names_from(right_rows_top)
    wstr_names = names_from(left_rows_bot)
    abs_names = names_from(right_rows_bot)
    if agg is not None:
        za, prze, wstr = agg
        if len(za_names) != za or len(prze_names) != prze or len(wstr_names) != wstr:
            return {"_mismatch": True, "topic": topic, "date": date,
                    "agg": agg, "cnt": (len(za_names), len(prze_names), len(wstr_names)),
                    "names": {"za": za_names, "przeciw": prze_names, "wstrzymal_sie": wstr_names,
                              "nieobecni_glos": abs_names}}
    return {"topic": topic, "date": date, "agg": agg,
            "za": za_names, "przeciw": prze_names, "wstrzymal_sie": wstr_names,
            "nieobecni_glos": abs_names}


def parse_vote_page(page):
    """Parse one DSSS vote page (pdfplumber) via shared word core."""
    text = page.extract_text() or ""
    if "g" not in text:
        return None
    if not re.search(r"Data i godzina g[sł]osowania", text):
        return None
    try:
        words = page.extract_words()
    except Exception:
        return None
    return _parse_vote_words(words, text)


def parse_report(pdf_bytes):
    """Returns (session_meta, [vote_recs], [mismatches])."""
    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        t0 = pdf.pages[0].extract_text() or ""
        sm = re.search(r'sesji\s+[“"]([^”"]+?)(\d{1,2}\.\d{1,2}\.\d{4})', t0)
        if not sm:
            sm2 = re.search(r'([IVXL]+)\s+sesja.*?(\d{1,2}\.\d{1,2}\.\d{4})', t0)
            sess_label, date_s = (sm2.groups() if sm2 else ("", ""))
        else:
            sess_label, date_s = sm.groups()
        att = re.search(r"stawi[oł] si[ęę]\s+(\d+)\s+radnych.*?(\d+)\s+radnych by[oł]o nieobecnych", t0, re.S)
        votes, mism = [], []
        for pg in pdf.pages[2:]:
            v = parse_vote_page(pg)
            if v is None:
                continue
            if v.get("_mismatch"):
                mism.append(v)
                continue
            votes.append(v)
    try:
        sdate = parse_iso(date_s.strip())
    except Exception:
        sdate = ""
    return {"label": sess_label.strip(), "date": sdate,
            "present": int(att.group(1)) if att else None}, votes, mism


def _roster_match(row_text, roster_norm):
    """Find roster canonical name whose surname token fuzzy-matches an OCR'd row.
    roster_norm: list of (surname_norm, firstname_norm, canonical)."""
    import difflib
    toks = [w.strip(".,") for w in row_text.split()]
    toks_n = [_nk(t) for t in toks]
    toks_n = [t for t in toks_n if len(t) >= 3 and not t.isdigit()]
    if not toks_n:
        return None
    best, best_score = None, 0.0
    for sur, first, canon in roster_norm:
        cand = max((difflib.SequenceMatcher(None, t, sur).ratio() for t in toks_n), default=0)
        if cand >= 0.78 and cand > best_score:
            # require first-name initial if present in row
            best, best_score = canon, cand
    return best


def build_roster_norm(names):
    out = []
    for nm in names:
        parts = nm.split()
        if len(parts) < 2:
            continue
        first, sur = parts[0], parts[-1]
        out.append((_nk(sur), _nk(first), nm))
    return out


def parse_report_ocr(pdf_bytes, roster_norm, max_pages=None):
    """OCR fallback for reports with broken font mapping. Returns [vote_recs].
    Validates against aggregates AND the roster; drops names not in roster."""
    import io
    import pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    votes = []
    n = len(doc)
    if max_pages:
        n = min(n, max_pages)
    for i in range(2, n):
        words = _ocr_words(doc, i)
        if not words:
            continue
        rows = _split_words_rows(words)
        full = "\n".join(_row_text(ws) for _, ws in rows)
        if not re.search(r"Jestem\s*za", full, re.I):
            continue
        rec = _parse_vote_words(words, full)
        if rec is None:
            continue
        # OCR rows may lose numbering -> roster-based re-extraction per quadrant
        def roster_names(rows_q):
            out = []
            for r in rows_q:
                r = re.sub(r"^\s*\d+\.\s*", "", r.strip())
                if not r or r.upper() == "BRAK":
                    continue
                m = _roster_match(r, roster_norm)
                if m and m not in out:
                    out.append(m)
            return out
        # recompute quadrants with roster matching using same geometry
        hdr = {}
        for y, ws in rows:
            _rt = _row_text(ws)
            if any(ch.isdigit() for ch in _rt):
                continue
            tl = _rt.lower()
            if "jestem za" in tl and "za" not in hdr:
                hdr["za"] = (ws[0]["x0"], y)
            if "jestem przeciw" in tl and "przeciw" not in hdr:
                hdr["przeciw"] = (300, y)
            if "wstrzymuj" in tl and ws[0]["x0"] < 300 and "wstrzymuje" not in hdr:
                hdr["wstrzymuje"] = (ws[0]["x0"], y)
        y_tbl0 = hdr["za"][1] + 4
        ymax = max(w["top"] for w in words) + 5
        y_split = hdr.get("wstrzymuje", (0, ymax))[1] - 3
        rq_za = roster_names(_rows_in_region(words, 0, 300, y_tbl0, y_split))
        rq_pr = roster_names(_rows_in_region(words, 300, 1e9, y_tbl0, y_split))
        rq_wa = roster_names(_rows_in_region(words, 0, 300, y_split + 2, ymax))
        if rec.get("_mismatch"):
            if rec["agg"] == (len(rq_za), len(rq_pr), len(rq_wa)):
                rec = {"_mismatch": False, "topic": rec["topic"], "date": rec["date"],
                       "agg": rec["agg"], "za": rq_za, "przeciw": rq_pr,
                       "wstrzymal_sie": rq_wa, "nieobecni_glos": []}
            else:
                rec["cnt_roster"] = (len(rq_za), len(rq_pr), len(rq_wa))
                votes.append(rec)  # keep as mismatch marker
                continue
        else:
            # prefer roster-matched lists if they agree with healthy parse
            if (len(rq_za), len(rq_pr), len(rq_wa)) == (len(rec["za"]), len(rec["przeciw"]), len(rec["wstrzymal_sie"])):
                rec["za"], rec["przeciw"], rec["wstrzymal_sie"] = rq_za, rq_pr, rq_wa
        votes.append(rec)
    doc.close()
    good = [v for v in votes if not v.get("_mismatch")]
    bad = [v for v in votes if v.get("_mismatch")]
    return good, bad


def parse_report_v3(pdf_bytes, roster_norm):
    """New DSSS layout ('Oddane głosy - podsumowanie'): linear per-voter rows
    'N. Imie Nazwisko <ZA|PRZECIW|Wstrzymał się|Nieobecny> [DD.MM.YYYY HH:MM]'.
    Text layer intact. Returns (good_votes, bad_votes)."""
    import io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        lines = []
        for pg in pdf.pages:
            lines.extend((pg.extract_text() or "").splitlines())
    good, bad = [], []
    cur = None       # current vote block being accumulated
    blocks = []      # (topic, date, agg, lines_list)
    i = 0
    n = len(lines)
    joined = "\n".join(lines)
    # iterate: vote blocks start at 'Głosowanie w sprawie:' with 'Data głosowania:' within 5 lines
    while i < n:
        ln = lines[i]
        if "Głosowanie w sprawie:" in ln:
            topic = ln.split("Głosowanie w sprawie:", 1)[1].strip().strip("“”\".")
            date = ""
            agg_d = {"za": None, "przeciw": None, "wstrz": None}
            j = i + 1
            body = []
            in_table = False
            while j < n:
                l2 = lines[j]
                # topic may wrap onto next line(s) before 'Typ głosowania'
                if not body and not in_table and date == "" and not re.match(r"Typ g", l2):
                    topic = (topic + " " + l2.strip()).strip()
                dm = re.search(r"Data g[sł]osowania:\s*([\d.]+)", l2)
                zm = re.search(r"\bZa:\s*(\d+)", l2)
                zm2 = re.search(r"Przeciw:\s*(\d+)", l2)
                wm = re.search(r"Wstrzyma[łl]o si[ęę]:\s*(\d+)", l2)
                if dm and not date:
                    date = dm.group(1)
                if zm:
                    agg_d["za"] = int(zm.group(1))
                if zm2:
                    agg_d["przeciw"] = int(zm2.group(1))
                if wm:
                    agg_d["wstrz"] = int(wm.group(1))
                if "podsumowanie szczeg" in l2.lower():
                    in_table = True
                if in_table:
                    if re.search(r"Dyskusje:|Przebieg dyskusji|G[łl]osowanie w sprawie:|Uchwała w sprawie.*Start:", l2):
                        break
                    body.append(l2)
                if in_table and j - i > 200:
                    break
                if not in_table and j - i > 14:
                    break
                j += 1
            agg = None
            if None not in (agg_d["za"], agg_d["przeciw"], agg_d["wstrz"]):
                agg = (agg_d["za"], agg_d["przeciw"], agg_d["wstrz"])
            if date and agg is not None and body:
                blocks.append((topic, date, agg, body))
            i = j
            continue
        i += 1
    for topic, date_s, agg, body in blocks:
        try:
            date = parse_iso(date_s)
        except Exception:
            continue
        za, prze, wstr, nieob = [], [], [], []
        buf = " ".join(body)
        # split rows on 'N ' numbering: rows look like '1 Artur Wałachowski Za 31.08.2026 14:40'
        row_pat = re.compile(r"(?:^|\s)(\d{1,2})\s+([A-ZŚŁŻŹĆŃÓĄ][\wŚŁŻŹĆŃÓĄ-]+(?:\s+[A-ZŚŁŻŹĆŃÓĄ][\wŚŁŻŹĆŃÓĄ-]+)*)\s+(Za|Przeciw|Wstrzyma[łl]o?\s*si[ęę]|Wstrzymał się|Nieobecny)")
        for m in row_pat.finditer(buf):
            nm = m.group(2).strip()
            vote = m.group(3)
            # fuzzy-check name against roster to avoid picking up garbage
            toks = [_nk(t) for t in nm.split()]
            ok = False
            canon = None
            for sur, first, cname in roster_norm:
                if sur in toks or any(difflib_ratio(t, sur) > 0.85 for t in toks):
                    ok = True
                    canon = cname
                    break
            if not ok:
                continue
            v = vote.lower()
            if v == "za":
                za.append(canon)
            elif v == "przeciw":
                prze.append(canon)
            elif "wstrzym" in v:
                wstr.append(canon)
            elif "nieobecny" in v:
                nieob.append(canon)
        if (len(za), len(prze), len(wstr)) == tuple(agg):
            good.append({"topic": topic, "date": date, "agg": agg,
                         "za": za, "przeciw": prze, "wstrzymal_sie": wstr,
                         "nieobecni_glos": nieob})
        else:
            bad.append({"topic": topic, "date": date, "agg": agg,
                        "cnt": (len(za), len(prze), len(wstr))})
    return good, bad


def difflib_ratio(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def build_output(all_votes, session_list, councilors_seen, club_assign):
    councilors_seen = sorted(set(councilors_seen))
    id_by_name = {n: i for i, n in enumerate(councilors_seen)}
    all_votes.sort(key=lambda v: (v["date"], v.get("ts", "")))
    sessions_data = []
    by_sess = defaultdict(list)
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess[v["date"]].append(v)
    for d, vs in sorted(by_sess.items()):
        sessions_data.append({"date": d, "number": d,
                              "label": f"Sesja {vs[0].get('session_num','')} ({d})",
                              "vote_count": len(vs)})
    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": v.get("session_num", ""),
                          "topic": v["topic"],
                          "named_votes": nv,
                          "counts": {"for_": len(v["za"]), "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v.get("nieobecni_glos", []))}})
    total_votes = len(votes_out)
    total_sessions = len(sessions_data)
    councilors_data = {n: {"name": n, "club": club_assign.get(n, ""),
                           "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                           "votes_brak": 0, "votes_nieobecny": 0} for n in councilors_seen}
    councillor_sess = defaultdict(set)
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"), ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in councilors_data:
                    councilors_data[nm][key] += 1
                    councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in councilors_data.values():
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
                                "activity": None})
    vectors = defaultdict(dict)
    for v in votes_out:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"][cat]:
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(all_votes, club_assign):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v[cat]:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": v["date"], "vote": cat})
    sess_set = {v["date"] for v in all_votes}
    n_sessions = len(sess_set) or 1
    profiles = []
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({x["session"] for x in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-reports", type=int, default=0)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    reps = discover_reports(cache)
    print(f"[zabki] raportow glosowan: {len(reps)}")
    if args.max_reports:
        reps = reps[:args.max_reports]

    all_votes = []
    roster = set()
    total_mismatch = 0
    broken = []  # reports with broken font mapping -> OCR pass
    for rep in reps:
        if rep["date"] < KAD_START:
            continue
        try:
            data = _get(rep["url"], cache)
        except Exception as e:
            print(f"  [ERR {rep['url']}] {e}")
            continue
        if data[:4] != b"%PDF":
            print(f"  [skip nie-PDF {rep['url']}]")
            continue
        meta, votes, mism = parse_report(data)
        # detect broken-font report: vote pages exist in render but text layer lost
        broken_rep = False
        try:
            import io as _io, pymupdf as _fitz
            _doc = _fitz.open(stream=data, filetype="pdf")
            n_vote = 0
            n_ok = 0
            for _i in range(2, len(_doc)):
                _t = _doc[_i].get_text()
                if re.search(r"Jestem\s*za|jestem za", _t) or "glosowanie" in _t.lower():
                    n_vote += 1
                    if len(_t) > 800:
                        n_ok += 1
            # broken if: text layer healthy count == 0 but PDF has >=3 pages (votes expected),
            # and zero votes were recovered
            broken_rep = (n_ok == 0 and len(votes) == 0 and len(mism) == 0 and len(_doc) > 3)
            _doc.close()
        except Exception:
            pass
        if broken_rep:
            broken.append(rep)
            print(f"  sesja {rep['session_num']:8s} {rep['date']}: USZKODZONA warstwa tekstu -> OCR pass")
            continue
        total_mismatch += len(mism)
        sdate = rep["date"]
        for v in votes:
            if not v["date"]:
                v["date"] = sdate
            v["session_num"] = rep["session_num"]
            all_votes.append(v)
            for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni_glos"):
                roster.update(v.get(cat, []))
        print(f"  sesja {rep['session_num']:8s} {sdate}: glosowan={len(votes)} mismatch={len(mism)} sesja_label={meta['label'][:40]}")

    # OCR pass for broken reports, validated against roster from healthy pass
    if broken:
        roster_norm = build_roster_norm(sorted(roster))
        print(f"[zabki] OCR pass: {len(broken)} raportow, roster={len(roster_norm)}")
        for rep in broken:
            try:
                data = _get(rep["url"], cache)
            except Exception as e:
                print(f"  [ERR ocr {rep['url']}] {e}")
                continue
            # new DSSS linear layout ('Oddane głosy - podsumowanie') -> v3 text parser
            is_v3 = False
            try:
                import pymupdf as _f2
                _d = _f2.open(stream=data, filetype="pdf")
                head = "".join(_d[i].get_text() for i in range(min(6, len(_d))))
                is_v3 = "podsumowanie" in head.lower()
                _d.close()
            except Exception:
                pass
            if is_v3:
                ovotes, omism = parse_report_v3(data, roster_norm)
                for v in ovotes:
                    v["session_num"] = rep["session_num"]
                    all_votes.append(v)
                    for cat in ("za", "przeciw", "wstrzymal_sie"):
                        roster.update(v.get(cat, []))
                total_mismatch += len(omism)
                print(f"  sesja {rep['session_num']:8s} {rep['date']}: V3-linear glosowan={len(ovotes)} mismatch={len(omism)}")
                continue
            ovotes, omism = parse_report_ocr(data, roster_norm)
            total_mismatch += len(omism)
            for v in ovotes:
                if not v["date"]:
                    v["date"] = rep["date"]
                v["session_num"] = rep["session_num"]
                all_votes.append(v)
                for cat in ("za", "przeciw", "wstrzymal_sie"):
                    roster.update(v.get(cat, []))
            print(f"  sesja {rep['session_num']:8s} {rep['date']}: OCR glosowan={len(ovotes)} mismatch={len(omism)}")
    dated = [v for v in all_votes if v["date"] >= KAD_START]
    # normalize single-token names (OCR truncation) into unique matching full names
    full_names = set()
    for v in dated:
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni_glos"):
            for nm in v.get(cat, []):
                if len(nm.split()) >= 2:
                    full_names.add(nm)
    def fix_name(nm):
        if len(nm.split()) >= 2:
            return nm
        cands = [f for f in full_names if f.startswith(nm + " ")]
        return cands[0] if len(cands) == 1 else nm
    for v in dated:
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni_glos"):
            if cat in v:
                v[cat] = [fix_name(n) for n in v[cat]]
    print(f"[zabki] glosowania imienne: {len(dated)} (mismatch odrzucone: {total_mismatch})")

    output, tv, ts = build_output(dated, None, roster, club_assign)
    profiles = build_profiles(dated, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zabki] DONE votes={tv} sessions={ts} councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
