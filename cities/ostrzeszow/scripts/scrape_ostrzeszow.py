#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ostrzeszów — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP ostrzeszow.biuletyn.net (platforma FLS/biuletyn.net, serwer-renderowany HTML),
kategoria cid=139 "Protokoły z sesji i wyniki głosowań". Per-sesja załącznik
"Wyniki głosowań z <N> sesji" = PDF TEKSTOWY eSesja-print: nagłówek sesji (Ostrzeszów + data),
per głos: numer+N. "Głosowanie ws. ..." + zagregaty (GŁOSOWAŁO: / głosowało ZA: / PRZECIW: /
WSTRZYMAŁO się:) + tabela "LP | Nazwisko i Imię | jak głosował" z wierszami
"1 Berezka Michał głosował ZA" (tokeny: głosował/głosowała ZA|PRZECIW, WSTRZYMAŁ się,
nie głosował, był nieobecny). Wiersze czyste liniowo (nie form-fill).
Obrót nazwisk: źródło "Nazwisko Imię" -> Radoskop "Imię Nazwisko".
Walidacja per głos: zliczone imienne == zagregaty (mismatch => odrzucenie głosu).

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python3 scrape_ostrzeszow.py --city-dir <dir> [--cache-dir dir]
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber required")

BIP = "https://ostrzeszow.biuletyn.net"
CATEGORY_URL = BIP + "/?bip=1&cid=139"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36"}
REQ_DELAY = 0.4
_LAST = 0.0

_ROMAN = re.compile(r"\b(X{0,2}(?:IX|IV|V?I{0,3}|IX|X?X?X?I?V?I{0,3}))\b")


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    req = urllib.request.Request(url, headers=UA)
    data = urllib.request.urlopen(req, timeout=60).read()
    if cache_dir:
        (Path(cache_dir) / (key + ".dat")).write_bytes(data)
    return data


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _roman_val(r):
    vals = {"I": 1, "V": 5, "X": 10}
    total, prev = 0, 0
    for ch in reversed(r):
        v = vals.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "pazdziernika": 10,
          "października": 10, "listopada": 11, "grudnia": 12}


def _to_roman(n):
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, sym in vals:
        while n >= v:
            out += sym
            n -= v
    return out


def _classify_vote(vote_words):
    """'głosował ZA' -> za; 'głosowała PRZECIW' -> przeciw; 'WSTRZYMAŁ się' -> wstrzymal_sie;
    'nie głosował' -> nie_glosowal; 'był nieobecny' -> nieobecni; 'był obecny' -> obecny."""
    k = _nk(" ".join(vote_words))
    if not k:
        return None
    if k.startswith("nieglosow"):
        return "nie_glosowal"
    if "nieobecn" in k:
        return "nieobecni"
    if k.startswith(("glosowalglosowtaglosowaliglosowaly", "glosowal", "glosowata", "glosowali", "glosowaly")):
        if k.endswith("za"):
            return "za"
        if "przeciw" in k:
            return "przeciw"
        return "za"  # samo "głosował" (rzadkie) traktuj po agregacie w walidacji
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("przeciw"):
        return "przeciw"
    if k == "za":
        return "za"
    if k.startswith("obecn"):
        return "obecny"
    return None


def parse_pdf(data, cache_note=""):
    """-> dict(date, votes=[{num,topic,agg{za,przeciw,wstrz,glosowalo},named{...}}])"""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        meta_lines = []
        for pi, page in enumerate(pdf.pages):
            words = page.extract_words()
            lines = defaultdict(list)
            for w in words:
                lines[round(w["top"])].append(w)
            merged = []  # [pi, top, ws]
            for top, ws in sorted(lines.items()):
                ws.sort(key=lambda w: w["x0"])
                if merged and merged[-1][0] == pi and top - merged[-1][1] <= 2:
                    merged[-1][2].extend(ws)
                    merged[-1][2].sort(key=lambda w: w["x0"])
                else:
                    merged.append([pi, top, ws])
            meta_lines.extend((pi, top, ws) for pi, top, ws in merged)
        # session date: search the whole joined text of all pages
        first = " ".join(w["text"] for _pi, _t, ws in meta_lines for w in ws)
        date = None
        m = re.search(r"(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|wrze[s]?nia|pa[s]?dziernika|listopada|grudnia)\s+(\d{4})", first)
        if m and m.group(2).lower() in MONTHS:
            date = f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        # session number from PDF body ("XXV Sesja" header) — anchor titles have typos
        sess_num = None
        msn = re.search(r"\b([IVXL]{1,6})\s+Sesja\b", first)
        if msn:
            sess_num = _roman_val(msn.group(1))
        votes = []
        cur = None
        agg = {}
        expect_agg = False
        for pi, top, ws in meta_lines:
            line_txt = " ".join(w["text"] for w in ws)
            lt = line_txt.strip()
            # topic start: "N. Głosowanie ..."
            mt = re.match(r"^(\d{1,3})\.\s+Glosowanie\s+(.*)", lt, re.I) or \
                 re.match(r"^(\d{1,3})\.\s+Głosowanie\s+(.*)", lt)
            if mt:
                cur = {"num": int(mt.group(1)),
                       "topic": re.sub(r"\s+", " ", mt.group(2)).strip(),
                       "agg": {}, "named": defaultdict(list)}
                votes.append(cur)
                agg = {}
                expect_agg = True
                continue
            if cur is not None and expect_agg:
                ma = re.search(r"(GŁOSOWAŁO|głosowało|WSTRZYMAŁO)\s*(?:się:|ZA:|PRZECIW:|:\s*)?\s*(\d+)", lt)
                if "GŁOSOWAŁO" in lt:
                    mnum = re.search(r"GŁOSOWAŁO:?\s*(\d+)", lt) or re.search(r"GŁOSOWAŁO:\s*$", lt)
                    if mnum:
                        cur["agg"]["glosowalo"] = int(mnum.group(1))
                    elif "GŁOSOWAŁO:" in lt:
                        # number may be on same line right side
                        nums = re.findall(r"\b(\d+)\b", lt)
                        if nums:
                            cur["agg"]["glosowalo"] = int(nums[-1])
                    continue
                if "ZA:" in lt:
                    nums = re.findall(r"\b(\d+)\b", lt)
                    if nums:
                        cur["agg"]["za"] = int(nums[-1])
                    continue
                if "PRZECIW:" in lt:
                    nums = re.findall(r"\b(\d+)\b", lt)
                    if nums:
                        cur["agg"]["przeciw"] = int(nums[-1])
                    continue
                if "WSTRZYMAŁO" in lt:
                    nums = re.findall(r"\b(\d+)\b", lt)
                    if nums:
                        cur["agg"]["wstrzymal_sie"] = int(nums[-1])
                    continue
                if "OBECNYCH" in lt.upper():
                    continue
                if "NIEOBECNI" in lt.upper():
                    continue
            # table row: LP number at x0<130, name words, vote words at x0>360
            if cur is not None and ws and re.match(r"^\d{1,2}$", ws[0]["text"]) and ws[0]["x0"] < 130:
                name_words = [w["text"] for w in ws if 128 < w["x0"] < 366]
                vote_words = [w["text"] for w in ws if w["x0"] >= 366]
                if not name_words or not vote_words:
                    continue
                # usuń zapożyczenia z nagłówka kolumny ("jak głosował") i etykiety "głosował/a"
                name_words = [w for w in name_words if _nk(w) not in ("jak",)]
                vote_words = [w for w in vote_words if _nk(w) not in ("jak", "glosowal", "glosowala", "glosowali", "glosowaly", "glosowaliglosowaly")]
                cat = _classify_vote(vote_words)
                if cat is None:
                    continue
                parts = " ".join(name_words).split()
                if len(parts) < 2:
                    continue
                name = " ".join(parts[1:] + parts[:1])  # źródło: Nazwisko Imię
                cur["named"][cat].append(name)
        # continuation topic lines appended to current topic before agg — skipped (topic truncated OK)
    return {"date": date, "votes": votes, "sess_num": sess_num}


def validate(v):
    agg = v["agg"]
    nm = v["named"]
    checks = []
    if "za" in agg and len(nm["za"]) != agg["za"]:
        return False
    if "przeciw" in agg and len(nm["przeciw"]) != agg["przeciw"]:
        return False
    if "wstrzymal_sie" in agg and len(nm["wstrzymal_sie"]) != agg["wstrzymal_sie"]:
        return False
    return any(len(nm[k]) for k in ("za", "przeciw", "wstrzymal_sie"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default="")
    args = ap.parse_args()
    city = Path(args.city_dir)
    docs = city / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    html = _get(CATEGORY_URL).decode("utf-8", "replace")
    pairs = re.findall(r'href="([^"]+\.pdf)"[^>]*>(.*?)</a>', html, re.S)
    results = {}
    for href, text in pairs:
        t = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()
        tn = _nk(t)
        if not tn.startswith("wyniki glosowan".replace(" ", "")) and "wynikiglosowan" not in tn:
            continue
        m2 = re.search(r"z\s+([IVXL]+)\s+sesji", t)
        if not m2:
            continue
        num = _roman_val(m2.group(1))
        url = href if href.startswith("http") else BIP + "/" + href.lstrip("/")
        parsed = parse_pdf(_get(url, args.cache_dir or None))
        if not parsed["date"] or not parsed["votes"]:
            print(f"[ostrzeszow] ses {num}: BRAK daty/glosowan — pomijam")
            continue
        if parsed["sess_num"]:
            num = parsed["sess_num"]  # tytul kotwicy bywa literowka zrodla
        if num in results:
            continue
        results[num] = parsed
        print(f"[ostrzeszow] ses {num} {parsed['date']}: {len(parsed['votes'])} glosowan "
              f"({sum(1 for v in parsed['votes'] if validate(v))} zwalid.)")

    all_votes = []
    seen_names = set()
    for num in sorted(results):
        s = results[num]
        if s["date"] < KAD_START:
            continue
        for v in s["votes"]:
            if not validate(v):
                continue
            nm = v["named"]
            for k in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal", "nieobecni"):
                seen_names.update(nm[k])
            all_votes.append({
                "date": s["date"], "session_num": num, "topic": v["topic"],
                "za": nm["za"], "przeciw": nm["przeciw"], "wstrzymal_sie": nm["wstrzymal_sie"],
                "nieobecni_glos": sorted(set(nm["nie_glosowal"]) | set(nm["nieobecni"]))})
    print(f"[ostrzeszow] sesji IX: {sum(1 for n in results if results[n]['date'] >= KAD_START)}, "
          f"glosowania: {len(all_votes)} radni: {len(seen_names)}")
    if len(all_votes) < 30:
        raise SystemExit(f"ZA MAŁO głosów ({len(all_votes)}) — przerywam")

    councilors_seen = sorted(seen_names)
    all_votes.sort(key=lambda v: (v["date"], v["session_num"]))
    sessions_data = []
    by_sess = defaultdict(list)
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess[v["date"]].append(v)
    for dd, vs in sorted(by_sess.items()):
        sessions_data.append({"date": dd, "number": dd,
                              "label": f"Sesja {_to_roman(vs[0]['session_num'])} ({dd})",
                              "vote_count": len(vs)})

    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": _to_roman(v["session_num"]),
                          "topic": v["topic"], "named_votes": nv,
                          "counts": {"for_": len(v["za"]), "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v["nieobecni_glos"])}})
    total_votes = len(votes_out)
    total_sessions = len(sessions_data)
    cdata = {n: {"name": n, "club": "", "votes_za": 0, "votes_przeciw": 0,
                 "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0}
             for n in councilors_seen}
    csess = defaultdict(set)
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                        ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in cdata:
                    cdata[nm][key] += 1
                    csess[nm].add(v["session_date"])
    councilors_list = []
    for cc in cdata.values():
        present = cc["votes_za"] + cc["votes_przeciw"] + cc["votes_wstrzymal"]
        councilors_list.append({
            "name": cc["name"], "club": "", "district": None,
            "frekwencja": round((len(csess.get(cc["name"], set())) / total_sessions * 100) if total_sessions else 0, 1),
            "aktywnosc": round((present / total_votes * 100) if total_votes else 0, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
            "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
            "votes_nieobecny": cc["votes_nieobecny"],
            "votes_total": present + cc["votes_nieobecny"],
            "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    def slugify(nm):
        s = unicodedata.normalize("NFKD", nm.lower())
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.replace("ł", "l")
        return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip().replace(" ", "-")

    profiles = {"profiles": [{"name": cc["name"], "slug": slugify(cc["name"]),
                              "kadencje": {KADENCJA_ID: {
                                  "club": cc["club"], "has_voting_data": True,
                                  "has_activity_data": False,
                                  "frekwencja": cc["frekwencja"], "aktywnosc": cc["aktywnosc"],
                                  "zgodnosc_z_klubem": 0.0,
                                  "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
                                  "votes_wstrzymal": cc["votes_wstrzymal"],
                                  "votes_brak": cc["votes_brak"],
                                  "votes_nieobecny": cc["votes_nieobecny"],
                                  "votes_total": cc["votes_total"],
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": [], "notes": "", "former": False,
                                  "mid_term": False}}}
                             for cc in councilors_list],
                "total": len(councilors_list)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ostrzeszow] ZAPISANO: {total_sessions} sesji, {total_votes} głosowań, "
          f"{len(councilors_list)} radnych")


if __name__ == "__main__":
    main()
