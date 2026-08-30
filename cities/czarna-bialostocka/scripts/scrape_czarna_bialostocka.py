#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Czarna Białostocka — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP https://bip-umczarnabialostocka.podlaskie.eu  (platforma podlaskie.eu,
sekcja /rada-miejska/ix-kadencja/glosowania/). Każda sesja = strona z jednym
załącznikiem PDF `/resource/{id}/Wykaz+głosowań+na+<N>+sesji...pdf` (format
posiedzenia.pl, TEKSTOWA warstwa). PDF per sesja zawiera LISTA RADNYCH + PORZĄDEK
OBRAD + per-głosowanie bloki: 'głosowanie <temat> … Podsumowanie ZA n / PRZECIW n /
WSTRZYMAŁO SIĘ n … Wyniki imienne' + tabela lp/nazwisko/imię/głos (GŁOS ∈
ZA / PRZECIW / WSTRZYMAŁ SIĘ / nieobecny(a)).

Walidacja: KAŻDE głosowanie reconcilowane vs agregat (suma list imiennych ==
ZA+PRZECIW+WSTRZYMAŁO z Podsumowania). Sesje IX kad. I..XXVII (2024-05-06 .. 2026-07-15).

Użycie:
    python scrape_czarna_bialostocka.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""
import argparse
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip-umczarnabialostocka.podlaskie.eu"
INDEX = f"{BASE}/rada-miejska/ix-kadencja/glosowania/"
KAD_START = "2024-05-01"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0"}
REQ_DELAY = 0.5
_LAST = 0.0

_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12,
        'wrzesnia': 9, 'pazdziernika': 10, 'sierpnia': 8}

_ROMAN = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8,
          'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
          'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20, 'xxi': 21,
          'xxii': 22, 'xxiii': 23, 'xxiv': 24, 'xxv': 25, 'xxvi': 26, 'xxvii': 27,
          'xxviii': 28, 'xxix': 29, 'xxx': 30, 'xxxi': 31, 'xxxii': 32, 'xxxiii': 33,
          'xxxiv': 34, 'xxxv': 35}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90, verify=False)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


def discover_sessions(cache):
    """Enumerate session pages from the glosowania index (IX kadencja only)."""
    html = _fetch(INDEX, cache)
    out = []
    seen = set()
    for m in re.finditer(r'href="(/rada-miejska/ix-kadencja/glosowania/glosowania-rady-miejskiej-na-([^"]+)\.html)"', html):
        page = m.group(1)
        if page in seen:
            continue
        seen.add(page)
        slug = m.group(2)
        # parse "xxvii-sesji-w-dniu-15-lipca-2026"
        ms = re.search(r'^([ivx]+)-sesji-w-dniu-(\d+)-([a-ząćęłńóśźż]+)-(\d{4})$', slug, re.I)
        if not ms:
            # malformed slug e.g. xv-sesji-w-dniu-28-sierpnia-czerwca-2025 -> recover from session number
            mn = re.match(r'^([ivx]+)-', slug, re.I)
            num = _ROMAN.get(mn.group(1).lower()) if mn else None
            if num:
                # malformed XV slug -> use the full original href (group(1))
                if num == 15:
                    date = "2025-08-28"
                    if date >= KAD_START:
                        fullpage = "/rada-miejska/ix-kadencja/glosowania/glosowania-rady-miejskiej-na-" + slug + ".html"
                        out.append({"page": BASE + fullpage, "date": date, "number": num})
            continue
        num = _ROMAN.get(ms.group(1).lower())
        day, mon, yr = int(ms.group(2)), _MON.get(ms.group(3).lower()), int(ms.group(4))
        if not mon:
            continue
        date = f"{yr}-{mon:02d}-{day:02d}"
        if date < KAD_START:
            continue
        out.append({"page": BASE + page, "date": date, "number": num})
    # dedupe by date (some slugs malformed like '28-sierpnia-czerwca-2025')
    by_date = {}
    for s in out:
        if s["date"] not in by_date:
            by_date[s["date"]] = s
    res = sorted(by_date.values(), key=lambda x: x["date"])
    return res


def session_resource_url(session_page, cache):
    """Extract the /resource/{id}/...pdf URL from a session page (double-encoded)."""
    html = _fetch(session_page, cache)
    m = re.search(r'href="(/resource/\d+/[^"\'<]*)"', html)
    if not m:
        return None
    path = m.group(1)
    # HTML is double-encoded (%25C5 -> %C5); collapse to single encoding
    single = path.replace("%25", "%")
    return BASE + single


def _norm_lines(text):
    """Normalize page text to a line list, merging wrapped vote tokens."""
    lines = text.split("\n")
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^Strona \d+ z \d+$", s):
            continue
        out.append(s)
    return out


def _vote_topic(block):
    """Extract topic from a vote text block."""
    # topic is between 'głosowanie' line and 'jednostka' line
    m = re.search(r"^głosowanie\s*\n(.*?)\njednostka", block, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _parse_aggregate(block):
    """Return (za, przeciw, wstrzymal) counts from Podsumowanie section."""
    za = re.search(r"^ZA\s*\n(\d+)", block, re.M)
    pr = re.search(r"^PRZECIW\s*\n(\d+)", block, re.M)
    wz = re.search(r"WSTRZYMAŁO\s*\nSIĘ\s*\n(\d+)", block)
    if not wz:
        wz = re.search(r"WSTRZYMAŁO SIĘ\s*\n(\d+)", block, re.M)
    z = int(za.group(1)) if za else None
    p = int(pr.group(1)) if pr else None
    w = int(wz.group(1)) if wz else None
    return z, p, w


def _parse_imienne(lines):
    """Parse 4-line imienne rows (lp / nazwisko / imię / głos) from a line list."""
    records = []
    i = 0
    n = len(lines)
    while i < n:
        # skip header row lp/nazwisko/imię/głos
        low = " ".join(lines[i:i+4]).lower()
        if ("lp" in low and "nazwisko" in low and "imię" in low) or ("imie" in low.lower()):
            i += 1
            continue
        # record = 4 lines: integer, surname, given, vote
        if i + 3 < n and re.match(r"^\d+$", lines[i]):
            lp = int(lines[i])
            surn = lines[i+1]
            given = lines[i+2]
            vote = lines[i+3]
            if lp >= 1 and surn and given and vote:
                records.append({"lp": lp, "name": f"{given} {surn}", "vote": vote})
                i += 4
                continue
        i += 1
    return records


def _extract_roster(full_lines):
    """Extract the councilor roster from the attendance block (LISTA RADNYCH / Obecni:).
    Returns list of 'Given Surname' names, order-un canonicalised."""
    txt = "\n".join(full_lines)
    names = []
    # pattern 1: 'Obecni:\n1. Eugeniusz Ciuruk\n2. ...'  (Raport z głosowań)
    m = re.search(r"Obecni\s*:\s*\n(.*?)(?:\n\s*\n|\nPrzeprowadzone|\n\s*Głosowano)", txt, re.S)
    if m:
        for line in m.group(1).split("\n"):
            mm = re.match(r"^\s*\d+\.\s*([A-ZŁŚŻŹĆŃĄĘÓ][^\d]{2,40})$", line.strip())
            if mm:
                names.append(mm.group(1).strip())
    # pattern 2: 'LISTA RADNYCH ... <lp> <nazwisko> <imię>' table (posiedzenia.pl)
    if not names:
        # from the attendance table: entries are lp/surname/given/status; reconstruct Given Surname
        i = 0
        n = len(full_lines)
        while i < n:
            l0 = full_lines[i].strip()
            if re.match(r"^\d+$", l0) and i + 3 < n:
                surn = full_lines[i+1].strip()
                given = full_lines[i+2].strip()
                st = full_lines[i+3].strip()
                if surn and given and st in ("obecny", "obecna", "nieobecny", "nieobecna"):
                    names.append(f"{given} {surn}")
                    i += 4
                    continue
            i += 1
    # dedupe preserving order
    seen = set()
    out = []
    for nm in names:
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def parse_pdf_votes(data, session_date, session_number):
    """Parse a session PDF into validated vote dicts. Handles TWO formats:
    - Table (posiedzenia.pl, 2025+): 'Podsumowanie ZA n ... Wyniki imienne' + lp/nazwisko/imię/głos rows
    - Ratap (Raport z głosowań, 2024): 'Wyniki imienne ZA (N): comma-separated names, ...
    Some sessions publish a .doc (OLE2, same table, UTF-16 text) instead of PDF.
    """
    # OLE2 compound .doc -> extract UTF-16 text, truncate binary header
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raw = data.decode("utf-16le", errors="ignore")
        idx = raw.find("WYKAZ GŁOSOWAŃ")
        if idx == -1:
            idx = raw.find("Wyniki imienne")
        if idx > 0:
            raw = raw[idx:]
        txt = raw.replace("\x07", "\n").replace("\r", "\n")
        full_lines = [l.strip() for l in txt.split("\n") if l.strip()]
        full_text = "\n".join(full_lines)
        roster = _extract_roster(full_lines)
        return _parse_table_format(full_lines, session_date, session_number), roster

    doc = pymupdf.open(stream=data, filetype="pdf")
    full_lines = []
    for i in range(doc.page_count):
        full_lines.extend(_norm_lines(doc[i].get_text()))
    full_text = "\n".join(full_lines)
    roster = _extract_roster(full_lines)

    if "Raport z głosowań" in full_text or re.search(r"^Głosowano w sprawie", full_text, re.M):
        return _parse_text_format(full_lines, full_text, session_date, session_number), roster
    return _parse_table_format(full_lines, session_date, session_number), roster


def _parse_text_format(full_lines, full_text, session_date, session_number):
    """Raport z głosowań: 'N. Głosowano w sprawie <temat> <data>, godz.<t>\nWyniki głosowania
    ZA: X, PRZECIW: Y, WSTRZYMUJĘ SIĘ: Z, BRAK GŁOSU: B, NIEOBECNI: N\nWyniki imienne(?) \nZA (N)\n<imiona,...>\n..."""
    votes = []
    # split into per-vote blocks at 'Głosowano w sprawie' marker (optional leading "N. ")
    blocks = re.split(r"(?m)(?=^\s*(?:\d+\.\s*)?G\u0142osowano w sprawie\b)", full_text)
    for blk in blocks:
        if "G\u0142osowano w sprawie" not in blk:
            continue
        za = re.search(r"ZA:\s*(\d+)", blk)
        pr = re.search(r"PRZECIW:\s*(\d+)", blk)
        wz = re.search(r"WSTRZYMUJĘ SIĘ:\s*(\d+)", blk)
        if not za:
            continue
        # aggregate
        z, p, w = int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0
        # named lists: header 'ZA (N)' then names until next header or footer
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        im = re.search(r"Wyniki imienne\s*:?\s*\n?(.*)", blk, re.S)
        if im:
            body = im.group(1)
            # stop at footer markers
            body = re.split(r"\n(?=(?:Uczestnictwo w g\u0142osowaniach|Wygenerowano za pomoc|Strona \d+ z))", body, maxsplit=1)[0]
            # if there is no explicit newline after imienne (names same line), body already starts at names
            # split into category sections
            sections = re.split(r"\n(?=(?:ZA|PRZECIW|WSTRZYMUJ\u0118 SI\u0118|BRAK G\u0141OSU|NIEOBECNI)\s*\()", body)
            for sec in sections:
                mcat = re.match(r"(ZA|PRZECIW|WSTRZYMUJ\u0118 SI\u0118|BRAK G\u0141OSU|NIEOBECNI)\s*\((\d+)\)\s*\n?(.*)", sec, re.S)
                if not mcat:
                    continue
                cat, cnt, names_txt = mcat.group(1), int(mcat.group(2)), mcat.group(3)
                # names wrap across lines mid-name (e.g. "Edyta\nPiekunko") -> join to one line, split on commas only
                names_txt = re.sub(r"\s*\n\s*", " ", names_txt)
                names = [n.strip() for n in re.split(r",\s*", names_txt) if n.strip()]
                if cat == "ZA":
                    named["za"] = [n for n in names if n]
                elif cat == "PRZECIW":
                    named["przeciw"] = [n for n in names if n]
                elif cat == "WSTRZYMUJ\u0118 SI\u0118":
                    named["wstrzymal_sie"] = [n for n in names if n]
                elif cat == "BRAK G\u0141OSU":
                    named["brak_glosu"] = [n for n in names if n]
                elif cat == "NIEOBECNI":
                    named["nieobecni"] = [n for n in names if n]
        counts = {k: len(v) for k, v in named.items()}
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if got != (z, p, w):
            continue
        # topic: after 'Głosowano w sprawie', up to ' <date>, godz.'
        tm = re.search(r"G\u0142osowano w sprawie\s*:?\s*(.+?)\s+\d{1,2} [a-z\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c]+ \d{4}, godz\.", blk)
        topic = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
        votes.append({"topic": topic, "named": named, "counts": counts,
                      "session_date": session_date,
                      "session_number": f"Sesja {session_number}" if session_number else ""})
    return votes


def _parse_table_format(full_lines, session_date, session_number):
    """posiedzenia.pl table format (2025+): 'Podsumowanie ... Wyniki imienne' + lp/nazwisko/imię/głos rows."""
    blocks = []
    cur = []
    for ln in full_lines:
        if ln.lower() == "głosowanie" and cur:
            blocks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)

    votes = []
    for b in blocks:
        text = "\n".join(b)
        za, pr, wz = _parse_aggregate(text)
        if za is None:
            continue
        wi = None
        for j, ln in enumerate(b):
            if ln.lower() in ("wyniki imienne", "wyniki imienne"):
                wi = j
                break
        if wi is None:
            continue
        im_lines = b[wi+1:]
        recs = _parse_imienne(im_lines)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        for r in recs:
            v = r["vote"].strip()
            if v == "ZA":
                named["za"].append(r["name"])
            elif v == "PRZECIW":
                named["przeciw"].append(r["name"])
            elif "WSTRZYMAŁ" in v.upper() or "WSTRZYMAL" in v.upper():
                named["wstrzymal_sie"].append(r["name"])
            elif "nieobecn" in v.lower():
                named["nieobecni"].append(r["name"])
            else:
                named["brak_glosu"].append(r["name"])
        counts = {k: len(v) for k, v in named.items()}
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        agg = (za, pr, wz) if (za is not None and pr is not None and wz is not None) else None
        if agg and got != agg:
            continue
        votes.append({"topic": _vote_topic(text), "named": named, "counts": counts,
                      "session_date": session_date,
                      "session_number": f"Sesja {session_number}" if session_number else ""})
    return votes


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _canon_name(name, given_set):
    """Given Surname canonicalisation: if 'Surname Given' order, swap to 'Given Surname'."""
    name = name.strip()
    if not name:
        return name
    parts = name.split()
    if len(parts) >= 2 and parts[-1] in given_set and parts[0] not in given_set:
        return " ".join(parts[-1:] + parts[:-1])
    return name


def build_output(records, session_map, roster=None):
    # ---- learn given names (for any residual 'Surname Given' from old text-format) ----
    given_counts = defaultdict(int)
    for rec in records:
        for names in rec["named"].values():
            for n in names:
                parts = n.split()
                if len(parts) >= 2:
                    given_counts[parts[0]] += 1
    given_set = {g for g, c in given_counts.items() if c >= 3}

    # Build roster-membership filter: canonical (given,surname) set from real roster
    roster_pairs = set()
    roster_names = set()
    for r in roster or []:
        r = _canon_name(r, given_set)
        roster_names.add(r)
        if len(r.split()) >= 2:
            roster_pairs.add(" ".join(sorted(w.lower() for w in r.split())))
    # fallback full-name lookup map (unused, kept for clarity)

    def keep(name):
        c = _canon_name(name, given_set)
        if c in roster_names:
            return True
        # fallback: all words of name are roster words
        if roster_pairs and len(c.split()) >= 2:
            pc = " ".join(sorted(w.lower() for w in c.split()))
            if pc in roster_pairs:
                return True
        return False

    # canonicalise + filter names in all records
    for rec in records:
        for k in list(rec["named"].keys()):
            filtered = [n for n in rec["named"][k] if keep(n)]
            rec["named"][k] = filtered
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": session_map.get(d, d),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": session_map.get(d, d),
            "topic": rec.get("topic", ""), "named_votes": rec["named"],
            "counts": rec["counts"],
        })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {}
    for name in all_names:
        councilors[name] = {"name": name, "club": "", "district": None,
                            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                            "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors:
                    if cat == "za":
                        councilors[nm]["votes_za"] += 1
                    elif cat == "przeciw":
                        councilors[nm]["votes_przeciw"] += 1
                    elif cat == "wstrzymal_sie":
                        councilors[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
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
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
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
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r.get("session_date") for r in records if (r.get("session_date") or "") >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        (out_path.parent / f"kadencja-{kid}.json").write_text(
            json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out_path.write_text(json.dumps({"generated": output.get("generated", ""),
                                    "default_kadencja": output.get("default_kadencja", ""),
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[czarna-bialostocka] sesje IX kadencji: {len(sessions)}")
    records = []
    roster_all = []
    session_map = {}
    for s in sessions:
        resource = session_resource_url(s["page"], cache)
        if not resource:
            print(f"  [skip {s['date']}] brak resource PDF")
            continue
        try:
            data = _fetch(resource, cache, binary=True)
        except Exception as e:
            print(f"  [err {s['date']}] {e}")
            continue
        try:
            votes, roster = parse_pdf_votes(data, s["date"], s["number"])
        except Exception as e:
            print(f"  [err pdf {s['date']}] {e}")
            continue
        for r in roster:
            if r not in roster_all:
                roster_all.append(r)
        session_map[s["date"]] = f"Sesja {s['number']}" if s["number"] else s["date"]
        print(f"  {s['date']} (sesja {s['number']}) votes_ok={len(votes)} roster={len(roster)}")
        records.extend(votes)
    output = build_output(records, session_map, roster_all)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    k = output["kadencje"][0]
    print(f"[czarna-bialostocka] total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")


if __name__ == "__main__":
    main()
