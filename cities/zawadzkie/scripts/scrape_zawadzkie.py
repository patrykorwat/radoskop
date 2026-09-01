#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Zawadzkie — imienne głosowania Rady Miejskiej w Zawadzkiem (IX kadencja).

Źródło: BIP bip.zawadzkie.pl (CMS idcom), kategorie "Posiedzenia sesji 2024/2025/2026 r."
(/4711/, /5136/, /5624/). Każda sesja = nagłówek "XX Sesja Rady Miejskiej w Zawadzkiem
DD miesiąc YYYY r." + załącznik "protokol-glosowan-...pdf" (system Rada365, WARSTWA
TEKSTOWA). PDF = wiele głosowań: temat "(HH:MM)" + "Wyniki imienne:" + listy
ZA (n) / PRZECIW (n) / WSTRZYMUJĘ SIĘ (n) / NIE GŁOSOWALI (n) / NIEOBECNI (n) —
nazwiska przecinkiem, zawijane wierszami BEZ przecinka; ostatnia lista zklejona z
tematem następnego głosowania (rozdzielone tylko brakiem przecinka) → parser
dwuprzebiegowy: pass-1 zbiera roster z list nie-ostatnich, pass-2 atrybuuje
ostatnie listy i tematy po nazwiskach rostru. Walidacja per głos: liczby == liczniki.
Skład/role: /4709/sklad-rady-miejskiej-w-zawadzkiem.html.

Użycie: python scrape_zawadzkie.py --city-dir <cities/zawadzkie> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from html import unescape
from itertools import combinations
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
try:
    from lib_names_pl import fix_all as _fix_all_names
except Exception:
    _fix_all_names = lambda xs: list(xs)

BASE = "https://bip.zawadzkie.pl"
PAGES = {2024: "/4711/posiedzenia-sesji-2024-r.html",
         2025: "/5136/posiedzenia-sesji-2025-r.html",
         2026: "/5624/posiedzenia-sesji-2026-r.html"}
SKLAD_URL = BASE + "/4709/sklad-rady-miejskiej-w-zawadzkiem.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.45
_LAST = 0.0

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "pazdziernika": 10,
           "października": 10, "listopada": 11, "grudnia": 12}
_ROM = {}
for _v, _r in [(1, "I"), (2, "II"), (3, "III"), (4, "IV"), (5, "V"), (6, "VI"), (7, "VII"),
               (8, "VIII"), (9, "IX"), (10, "X"), (11, "XI"), (12, "XII"), (13, "XIII"),
               (14, "XIV"), (15, "XV"), (16, "XVI"), (17, "XVII"), (18, "XVIII"), (19, "XIX"),
               (20, "XX"), (21, "XXI"), (22, "XXII"), (23, "XXIII"), (24, "XXIV"),
               (25, "XXV"), (26, "XXVI"), (27, "XXVII"), (28, "XXVIII"), (29, "XXIX"),
               (30, "XXX"), (31, "XXXI"), (32, "XXXII"), (33, "XXXIII")]:
    _ROM[_r] = _v

FOOTER_RE = re.compile(r"wydrukowano|wygenerowano|strona|systemu\s+Rada365|^Gmina\s", re.I)
NAME_RE = re.compile(r"^[A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\-]*(?: [A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\.\-]*){1,2}$")

LABEL_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
             "NIE GŁOSOWALI": "brak_glosu", "NIE GŁOSOWALI/NIEOBECNI": "brak_glosu",
             "NIEOBECNI": "nieobecni"}
LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJ[EĘ] SI[EĘ]|NIE G\u0141OSOWALI(?:/NIEOBECNI)?|NIE GLOSOWALI(?:/NIEOBECNI)?|NIEOBECNI)\s*\((\d+)\)")
OCC_RE = re.compile(r"Wyniki imienne\s*:")


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    ext = ".bin" if binary else ".html"
    cf = None
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + 2 * attempt)
    data = r.content
    if cf is not None:
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data.decode("utf-8", "ignore"), encoding="utf-8")
    return data if binary else data.decode("utf-8", "ignore")


def _clean_name(tok):
    tok = re.sub(r"\s+", " ", tok).strip(" ,;.")
    if not tok or FOOTER_RE.search(tok):
        return None
    if not NAME_RE.match(tok):
        return None
    return tok


def pdf_text(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    raw = "\n".join(p.get_text() for p in doc)
    doc.close()
    lines = [l for l in raw.split("\n")
             if not re.search(r"wydrukowano|Strona\s|Strona$|systemu\s+Rada365|^Gmina\s+\S+\s*$", l, re.I)]
    return "\n".join(lines)


def _chunks(body):
    """Yield (norm_label, expect, chunk_clean, is_last) for one vote body."""
    lab_iter = list(LABEL_RE.finditer(body))
    for j, lm in enumerate(lab_iter):
        label = lm.group(1).upper().replace("Ę", "E").replace("Ł", "L")
        norm = None
        for k, v in LABEL_MAP.items():
            if k.upper().replace("Ę", "E").replace("Ł", "L") == label:
                norm = v
                break
        if norm is None:
            continue
        chunk_end = lab_iter[j + 1].start() if j + 1 < len(lab_iter) else len(body)
        chunk = re.sub(r"\s+", " ", body[lm.end():chunk_end]).strip()
        yield norm, int(lm.group(2)), chunk, (j == len(lab_iter) - 1), lab_iter


def parse_legacy_text(data_or_text, roster):
    """Starszy format 'PROTOKÓŁ GŁOSOWANIA' (sesje I..VIII 2024, ten sam BIP): per-page
    bloki; nazwisko i token głosu w osobnych wierszach. Atrybucja per wiersz rosterem,
    walidacja agregatami (ZA/PRZECIW/WSTRZYMAŁO header + 'Liczba nieoddanych')."""
    if isinstance(data_or_text, (bytes, bytearray)):
        doc = pymupdf.open(stream=data_or_text, filetype="pdf")
        pages = [p.get_text() for p in doc]
        doc.close()
    else:
        pages = data_or_text.split("\x0c")
    rosters = sorted(roster, key=len, reverse=True)
    vote_re = re.compile(r"^(ZA|PRZECIW|WSTRZYMA[oŁ] SI[EĘ]|NIE G\u0141OSUJE|BRAK|NIEOBECN\w*)$", re.I)
    votes = []
    for pg in pages:
        if "PROTOKÓŁ GŁOSOWANIA" not in pg.upper():
            continue
        gm = re.search(r"G\u0142osy oddane\s*:", pg, re.I)
        if not gm:
            continue
        # expected counts: header table row 'ZA PRZECIW WSTRZYMAŁO SIĘ' then 3 ints lines
        counts = re.findall(r"(?m)^(\d{1,2})\s*$", pg[:gm.start()])
        agg = re.search(r"Liczba nieoddanych\u0142?ych?\s+g\u0142os[oó]w:\s*(\d+)", pg, re.I) or \
              re.search(r"Liczba nieoddanych\s+g\u0142os[oó]w:\s*(\d+)", pg, re.I)
        expect_absent = int(agg.group(1)) if agg else 0
        za_e = int(counts[-3]) if len(counts) >= 3 else None
        pr_e = int(counts[-2]) if len(counts) >= 2 else None
        ws_e = int(counts[-1]) if counts else None
        # topic: lines between page header block and the count lines
        tzone = pg[gm.start():]  # placeholder
        tm = re.search(r"WSTRZYMA[oŁ] SI[EĘ]\s*\n((?:\d{1,2}\s*\n){3})", pg, re.I)
        topic_m = re.search(r"Przedmiot głosowania\s*\n(.*?)\n\d{1,2}\s*\n\d{1,2}\s*\n\d{1,2}", pg, re.S)
        topic = re.sub(r"\s+", " ", topic_m.group(1)).strip(" .") if topic_m else ""
        # names after 'Głosy oddane:' — iterate lines: name line then vote token
        rest = pg[gm.end():]
        # cut at footer 'N / M'
        rest = re.split(r"(?m)^\s*\d{1,2}\s*/\s*\d{1,2}\s*$", rest)[0]
        lines = [l.strip() for l in rest.split("\n")]
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
        cur_name = None
        for l in lines:
            if not l:
                continue
            if vote_re.match(l):
                tok = l.upper()
                if not cur_name:
                    continue
                if tok == "ZA":
                    named["za"].append(cur_name)
                elif tok.startswith("PRZECIW"):
                    named["przeciw"].append(cur_name)
                elif tok.startswith("WSTRZYMA"):
                    named["wstrzymal_sie"].append(cur_name)
                else:
                    named["brak_glosu"].append(cur_name)
                cur_name = None
                continue
            nm = None
            for cand in rosters:
                if l == cand:
                    nm = cand
                    break
            if nm:
                cur_name = nm
            elif cur_name:
                cur_name = None  # wrapped name continuation w/o roster hit -> drop pair
        ok = True
        if za_e is not None and len(named["za"]) != za_e:
            ok = False
        if pr_e is not None and len(named["przeciw"]) != pr_e:
            ok = False
        if ws_e is not None and len(named["wstrzymal_sie"]) != ws_e:
            ok = False
        total_named = sum(len(v) for v in named.values())
        if ok and (not named["za"] and not named["przeciw"] and not named["wstrzymal_sie"]):
            ok = False
        if not ok or total_named + expect_absent == 0:
            continue
        topic = re.sub(r"^\s*ZA\s+PRZECIW\s+WSTRZYMA[oŁ]O?\s+SI[EĘ]\s*", "", topic, flags=re.I)
        topic = re.sub(r"^\d{1,2}\s+", "", topic)
        topic = topic.strip(" .")
        votes.append({"topic": topic or "głosowanie", "time": "", "named": named})
    return votes


def roster_pass(texts, seed):
    """Pass-1: names from NON-LAST label chunks (unpolluted by next topic) + legacy tokens."""
    roster = set(seed)
    legacy_name = re.compile(r"([A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\-]+(?: [A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\-]+)+)\s+(?:ZA|PRZECIW|WSTRZYMA[oŁ]\s+SI[EĘ])\b")
    for text in texts:
        for mm in legacy_name.finditer(text):
            if "\n" in mm.group(1):
                continue  # name/token split across lines (legacy per-line layout)
            n = _clean_name(mm.group(1))
            if n:
                roster.add(n)
        if "Wyniki imienne" not in text:
            continue
        occ = [m.start() for m in OCC_RE.finditer(text)]
        for i, pos in enumerate(occ):
            nxt = occ[i + 1] if i + 1 < len(occ) else len(text)
            body = text[pos:nxt]
            for norm, expect, chunk, is_last, _li in _chunks(body):
                if is_last or expect <= 0:
                    continue
                for tok in chunk.split(","):
                    n = _clean_name(tok)
                    if n:
                        roster.add(n)
    return sorted(roster, key=len, reverse=True)


def parse_text(text, roster):
    """Pass-2: full parse of one session PDF with roster anchoring."""
    if "Wyniki imienne" not in text:
        return []
    rosters = sorted(roster, key=len, reverse=True)
    votes = []
    occ = [m.start() for m in OCC_RE.finditer(text)]
    for i, pos in enumerate(occ):
        nxt = occ[i + 1] if i + 1 < len(occ) else len(text)
        body = text[pos:nxt]
        prev_start = occ[i - 1] if i > 0 else 0
        pre = text[prev_start:pos]
        anchors = list(re.finditer(r"\((\d{1,2}):(\d{2})\)", pre))
        when = ""
        topic = ""
        if anchors:
            a = anchors[-1]
            when = f"{a.group(1)}:{a.group(2)}"
            zone = pre[:a.start()]
            end = -1
            for nm in rosters:
                j = zone.rfind(nm)
                if j != -1 and j + len(nm) > end:
                    end = j + len(nm)
            topic = zone[end:] if end != -1 else zone[-250:]
        else:
            topic = pre
        topic = re.sub(r"\s+", " ", topic).strip(" .")
        topic = re.sub(r"^(Wykaz głosowań sesji(/posiedzenia)?\s*-\s*)?(X*[IVX]*\s*Sesja [^.]*?\.)?\s*", "", topic)
        topic = re.sub(r"^Wyniki imienne:?\s*", "", topic).strip(" .")
        # strip glued aggregate header remnants like 'PRZECIW (0) WSTRZYMUJĘ SIĘ (0) ...'
        prev_t = None
        while prev_t != topic:
            prev_t = topic
            topic = re.sub(r"^(ZA|PRZECIW|WSTRZYMUJ[EĘ] SI[EĘ]|NIE G\u0141OSOWALI(?:/NIEOBECNI)?|NIE GLOSOWALI(?:/NIEOBECNI)?|NIEOBECNI)\s*\(\d+\)[:\s]*", "", topic, flags=re.I).strip()
        named = {}
        ok = True
        seen_last = False
        for norm, expect, chunk, is_last, _li in _chunks(body):
            names = []
            if expect > 0:
                hits = []
                for nm in rosters:
                    p = chunk.find(nm)
                    if p != -1:
                        hits.append((p, nm))
                hits.sort()
                ded = []
                last_end = -1
                for p, nm in hits:
                    if p >= last_end:
                        ded.append((p, nm))
                        last_end = p + len(nm)
                names = [nm for _p, nm in ded][:expect]
                if len(names) != expect:
                    names = [n for n in (_clean_name(t) for t in chunk.split(",")) if n]
            if is_last:
                seen_last = True
            named[norm] = names[:expect]
            if len(named[norm]) != expect:
                ok = False
        if not ok or not seen_last:
            continue
        if not all(k in named for k in ("za", "przeciw", "wstrzymal_sie")):
            continue
        votes.append({"topic": topic or "głosowanie", "time": when, "named": named})
    return votes


def discover_sessions():
    sessions = []
    for _yr, pg in PAGES.items():
        t = _fetch(BASE + pg)
        body = t[t.find("bip-page__content"):] if "bip-page__content" in t else t
        body = body.replace("&nbsp;", " ")
        tokens = []
        for m in re.finditer(r"\b([IVXLCDM]{1,7})\s+Sesja[^<]{0,140}?(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|wrze[sś]nia|pazdziernika|października|listopada|grudnia)\s+(\d{4})", body):
            tokens.append(("sess", m.start(), m.group(1), (m.group(2), m.group(3), m.group(4))))
        for m in re.finditer(r'<a class="fileLink" href="(https://bip\.zawadzkie\.pl/download/attachment/\d+/[^"]*glosowan[^"]*|https://bip\.zawadzkie\.pl/download/attachment/\d+/[^"]*g\u0142osowan[^"]*)"', body):
            tokens.append(("att", m.start(), unescape(m.group(1)), None))
        tokens.sort(key=lambda x: x[1])
        cur = None
        for kind, _pos, a, b in tokens:
            if kind == "sess":
                mm = b
                month = _MONTHS.get(mm[1].lower().replace("ś", "s"))
                if not month:
                    continue
                date = f"{mm[2]}-{month:02d}-{int(mm[0]):02d}"
                num = _ROM.get(a.upper().strip())
                cur = {"num": num, "date": date, "att": None}
                sessions.append(cur)
            elif kind == "att" and cur is not None and cur["att"] is None:
                cur["att"] = a
    out = []
    seen = set()
    for s in sessions:
        k = (s["num"], s["date"])
        if s["date"] < KAD_START or not s["att"] or k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def scrape_sklad():
    t = _fetch(SKLAD_URL)
    t = unescape(t)
    body = t[t.find("bip-page__content"):]
    txt = re.sub(r"<[^>]+>", "\n", body)
    lines = [re.sub(r"\s+", " ", l.replace("\xa0", " ")).strip() for l in txt.split("\n")]
    lines = [l for l in lines if l]
    roster = {}
    for i, l in enumerate(lines):
        if re.fullmatch(r"\d{1,2}\.", l):
            # next non-empty, non-nbsp line = name
            for k in range(i + 1, min(i + 4, len(lines))):
                cand = lines[k].strip()
                if not cand or cand == "&nbsp;":
                    continue
                if NAME_RE.match(cand) and len(cand.split(" ")) >= 2:
                    role = ""
                    for k2 in range(k + 1, min(k + 3, len(lines))):
                        n = lines[k2].strip()
                        if n and n != "&nbsp;":
                            if re.match(r"^(Przewodniczą|Wiceprzewodniczą|Radny|Radna|Członek|Zastępca)", n):
                                role = n.rstrip(" .")
                            break
                    roster.setdefault(cand, role)
                break
    return roster


def slugify(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else city_dir / ".cache"
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    sessions = discover_sessions()
    print(f"[zawadzkie] sessions IX: {len(sessions)}")
    pdfs = {}
    for s in sessions:
        pdfs[s["date"]] = pdf_text(_fetch(s["att"], cache=cache, binary=True))
    skl = scrape_sklad()
    print(f"[zawadzkie] sklad page: {len(skl)} names")
    roster = roster_pass(list(pdfs.values()), list(skl.keys()))
    print(f"[zawadzkie] roster pass-1: {len(roster)}")

    votes_all = []
    council_stats = defaultdict(lambda: defaultdict(int))
    for s in sessions:
        votes = parse_text(pdfs[s["date"]], roster)
        if not votes:
            votes = parse_legacy_text(_fetch(s["att"], cache=cache, binary=True), roster)
        print(f"  sesja {s['num']} {s['date']}: {len(votes)} glosowan")
        for vi, v in enumerate(votes, 1):
            nv = v["named"]
            za, vo, ws = nv.get("za", []), nv.get("przeciw", []), nv.get("wstrzymal_sie", [])
            bg, nb = nv.get("brak_glosu", []), nv.get("nieobecni", [])
            attendees = set(za) | set(vo) | set(ws) | set(bg)
            for n in za:
                council_stats[n]["za"] += 1
            for n in vo:
                council_stats[n]["przeciw"] += 1
            for n in ws:
                council_stats[n]["wstrzymal_sie"] += 1
            for n in bg:
                council_stats[n]["brak"] += 1
            for n in nb:
                council_stats[n]["nieobecny"] += 1
            votes_all.append({
                "id": f"{s['date'].replace('-', '')}-{s['num']}-{vi}",
                "title": v["topic"],
                "date": s["date"],
                "session_num": s["num"],
                "session_date": s["date"],
                "attendee_count": len(attendees),
                "named_votes": {"za": za, "przeciw": vo, "wstrzymal_sie": ws,
                                 "nie_glosowali": bg, "nieobecni": nb},
                "result": "przyjete" if len(za) > len(vo) else "odrzucone",
            })
    names_union = set(skl.keys())
    for vv in votes_all:
        for lst in vv["named_votes"].values():
            names_union |= set(lst)
    # normalize 'Nazwisko Imię' -> 'Imię Nazwisko' and merge duplicates
    canon = {}
    for n in _fix_all_names(sorted(names_union)):
        canon.setdefault(n, n)
    swap = {}
    for n in names_union:
        c = canon.get(_fix_all_names([n])[0], n)
        swap[n] = c
    for vv in votes_all:
        vv["named_votes"] = {k: [swap.get(x, x) for x in lst] for k, lst in vv["named_votes"].items()}
    council_stats2 = defaultdict(lambda: defaultdict(int))
    for vv in votes_all:
        nvk = vv["named_votes"]
        for n in nvk["za"]:
            council_stats2[n]["za"] += 1
        for n in nvk["przeciw"]:
            council_stats2[n]["przeciw"] += 1
        for n in nvk["wstrzymal_sie"]:
            council_stats2[n]["wstrzymal_sie"] += 1
        for n in nvk["nie_glosowali"]:
            council_stats2[n]["brak"] += 1
        for n in nvk["nieobecni"]:
            council_stats2[n]["nieobecny"] += 1
    council_stats = council_stats2
    names_union = {swap.get(n, n) for n in names_union}
    skl = {swap.get(k, k): v for k, v in skl.items()}
    all_names = sorted(names_union)
    print(f"[zawadzkie] votes: {len(votes_all)}, names: {len(all_names)}")

    by_sess_date = defaultdict(list)
    for vv in votes_all:
        by_sess_date[vv["session_date"]].append(vv)
    sess_list = []
    for s in sessions:
        sv = by_sess_date.get(s["date"], [])
        if not sv:
            continue
        sess_list.append({"id": f"sesja-{s['num']}", "number": str(s["num"]),
                          "date": s["date"],
                          "label": f"{s['num']} Sesja Rady Miejskiej ({s['date']})",
                          "vote_count": len(sv)})

    pairs = defaultdict(lambda: [0, 0])
    for vv in votes_all:
        v = {}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for n in vv["named_votes"].get(cat, []):
                v[n] = cat
        ns = sorted(v)
        for a, b in combinations(ns, 2):
            pairs[(a, b)][1] += 1
            if v[a] == v[b]:
                pairs[(a, b)][0] += 1
    sim = {}
    for n in all_names:
        vals = [100.0 * c[0] / c[1] for k, c in pairs.items() if n in k and c[1] >= 5]
        sim[n] = round(sum(vals) / len(vals), 1) if vals else None

    councilors = []
    for n in all_names:
        st = council_stats.get(n, {})
        cast = st.get("za", 0) + st.get("przeciw", 0) + st.get("wstrzymal_sie", 0)
        present = cast + st.get("brak", 0)
        councilors.append({
            "name": n, "slug": slugify(n), "club": "", "role": skl.get(n, ""),
            "frekwencja": round(100.0 * present / len(votes_all), 1) if votes_all else 0,
            "aktywnosc": round(100.0 * cast / len(votes_all), 1) if votes_all else 0,
            "votes": cast,
            "zgodnosc_z_izba": sim.get(n),
        })

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "sessions": sess_list, "votes": votes_all,
        "councilor_index": all_names, "councilors": councilors,
        "total_councilors": len(all_names), "total_votes": len(votes_all),
        "total_sessions": len(sess_list),
        "similarity_top": sorted([{"name": n, "value": s} for n, s in sim.items() if s is not None],
                                  key=lambda x: -x["value"])[:10],
        "similarity_bottom": sorted([{"name": n, "value": s} for n, s in sim.items() if s is not None],
                                     key=lambda x: x["value"])[:10],
    }
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = []
    for c in councilors:
        profiles.append({
            "name": c["name"], "slug": c["slug"], "club": c["club"], "role": c["role"],
            "photo_url": "", "bio": "", "email": "", "social_links": {}, "voting": None,
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True, "role": c["role"],
                "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
                "zgodnosc_z_klubem": None, "zgodnosc_z_izba": c["zgodnosc_z_izba"],
                "rebellion_count": 0,
            }},
        })
    (docs / "profiles.json").write_text(json.dumps(
        {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": profiles,
         "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "city": "Zawadzkie", "rada": "Rada Miejska w Zawadzkiem",
        "kadencja_active": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "stats": {"total_votes": len(votes_all), "total_sessions": len(sess_list),
                  "total_councilors": len(all_names)},
        "source": {"bip": BASE, "type": "Rada365 protokol glosowan PDF (warstwa tekstowa)"},
    }
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zawadzkie] DONE: {len(sess_list)} sesji, {len(votes_all)} glosowan, {len(all_names)} radnych")


if __name__ == "__main__":
    main()
