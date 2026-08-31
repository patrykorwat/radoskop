#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Zbąszyń — imienne głosowania Rady Miejskiej (e-zeto BIP, skany protokołów, OCR).

Źródło: https://bip.zbaszyn.pl (umzbaszyn.bip.e-zeto.eu — ten sam silnik)
  Kategoria "Protokoły z sesji kadencja 2024-2029" (bt57, mnu8/70), paginacja
  POST-em pola dispNaviBar (3 strony po ~8 pozycji).
  Pozycja = strona z linkiem do pliku PDF protokołu (skan, brak warstwy tekstu).
  PDF = protokół z pełnymi "Wyniki głosowania: ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n,
  BRAK GŁOSU: n, NIEOBECNI: n" + tabela "Wyniki imienne:" (lp | nazwisko imię | głos).

OCR: fitz render dpi=110 + tesseract -l pol (serial!). Atrybucja per radny mapowana
słownikowo na roster IX kadencji (15 radnych, BIP menu "Radni Kadencji 2024 - 2029")
— fuzzy korekta literówek OCR nazwisk (Krystiane->Krystianc).

Walidacja: KAŻDE głosowanie reconcilowane vs agregat (za/przeciw/wstrz == liczby
list imiennych); nie-reconcilowane pomijane.

Użycie: python scrape_zbaszyn.py --city-dir cities/zbaszyn [--cache-dir /cache/zbaszyn]
"""
import argparse
import difflib
import hashlib
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf

BASE = "https://bip.zbaszyn.pl/index.php"
BIP = "https://bip.zbaszyn.pl/"
CAT0, CAT1 = "mnu8", "70"   # bt57 Protokoły z sesji kadencja 2024-2029
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024\u20132029)"
KAD_START = "2024-05-07"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
REQ_DELAY = 0.5
_LAST = 0.0

# Roster IX kadencji — BIP menu "Radni Kadencji 2024 - 2029" (zweryfikowany 2026-08-31)
ROSTER = [
    "Paweł Michałowski", "Katarzyna Rzepa", "Anna Drogla", "Łukasz Tarczewski",
    "Łukasz Szaferski", "Marek Sołtysik", "Iwona Samczuk", "Monika Niezborała",
    "Marzena Nowacka", "Grzegorz Nowaczyk", "Stefan Napierała", "Przemysław Krzyżaniak",
    "Marianna Krystianc", "Arleta Konopa", "Ryszard Zając",
]
ROLES = {"Paweł Michałowski": "Przewodniczący Rady Miejskiej",
         "Katarzyna Rzepa": "Wiceprzewodnicząca Rady Miejskiej"}
# słownik nazwisk (bez diakrytyków) -> pełne imię i nazwisko
_SUR = {unicodedata.normalize("NFKD", n.split()[-1]).encode("ascii", "ignore").decode().lower(): n for n in ROSTER}

_MON = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
        "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}
_R = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s):
    total, prev = 0, 0
    for ch in reversed(s.strip()):
        v = _R.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"ł", "l", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def fetch(url, binary=False, cache=None, data=None):
    _rate()
    key = hashlib.md5(url.encode() + (data if isinstance(data, bytes) else (data or "").encode())).hexdigest()
    if cache:
        cf = cache / (key + (".pdf" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    req = urllib.request.Request(url, headers=UA, data=data)
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        raw = r.read()
    if binary:
        if cache:
            (cache / (key + ".pdf")).write_bytes(raw)
        return raw
    txt = raw.decode("utf-8", "replace")
    if cache:
        (cache / (key + ".html")).write_text(txt, encoding="utf-8")
    return txt


def item_pages(cache):
    """Enumerate bt29 item ids via dispNaviBar POST pagination (pages 1..4)."""
    name = (f"type=4&name=bt1&func=dispNaviBar&value%5B0%5D={CAT0}&value%5B1%5D={CAT1}"
            f"&param%5Bwartosc%5D%5B0%5D={CAT0}&param%5Bwartosc%5D%5B1%5D={CAT1}"
            "&param%5Bfunkcja%5D=selectsite&param%5Bsortuj%5D%5Bkierunek%5D=DESC"
            "&param%5Bsortuj%5D%5Bsort1%5D=datad&param%5Bsortuj%5D%5Bsort2%5D=numer"
            f"&param%5Bsortuj%5D%5Bsort3%5D=nazwa&param%5B0%5D={{P}}")
    ids = []
    for p in range(4):
        body = (urllib.parse.quote(name.replace("{P}", str(p)), safe="") + "=" + str(p + 1)).encode()
        try:
            t = fetch(BASE, cache=cache, data=body)
        except Exception as e:
            print(f"  [warn] page {p+1}: {e}")
            break
        got = sorted(set(re.findall(r"bt29%26func%3Dselectsite%26value%255B0%255D%3D(\d+)", t)), key=int)
        if not got:
            break
        for i in got:
            if i not in ids:
                ids.append(i)
        if len(got) < 8:
            break
    return ids


def item_pdf_url(iid, cache):
    """From a bt29 item page, extract protocol PDF URL (relative fckeditor path or downloadFile)."""
    t = fetch(f"{BASE}?type=4&name=bt29&func=selectsite&value%5B0%5D={iid}", cache=cache)
    i = t.find('id="TRESC"')
    body = t[i:i + 40000] if i > 0 else t
    m = re.search(r'href="([^"]+\.pdf[^"]*)"', body, re.I)
    if not m:
        return None, ""
    href = m.group(1).replace("&amp;", "&")
    if href.startswith("http"):
        url = href
    elif href.startswith("index.php"):
        url = BIP + href
    else:
        url = BIP + urllib.parse.quote(href, safe="/:&=?%")
    dm = re.search(r"odby[łl]a si[ęe] w dniu (\d{1,2})\s+(\w+)\s+(\d{4})", body)
    date_iso = ""
    if dm and dm.group(2).lower() in _MON:
        date_iso = f"{dm.group(3)}-{_MON[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
    return url, date_iso


def ocr_pdf(raw, cache=None, url=""):
    """Full-text OCR of a scanned PDF (tesseract -l pol, serial pages). Cached."""
    if cache and url:
        cf = cache / (hashlib.md5(url.encode()).hexdigest() + ".ocr.txt")
        if cf.is_file():
            return cf.read_text(encoding="utf-8")
    doc = pymupdf.open(stream=raw, filetype="pdf")
    pages = []
    with tempfile.TemporaryDirectory() as td:
        for i, pg in enumerate(doc):
            pix = pg.get_pixmap(dpi=150)
            p = Path(td) / f"p{i}.png"
            pix.save(str(p))
            out = subprocess.run(["tesseract", str(p), "-", "-l", "pol", "--psm", "6"],
                                 capture_output=True, text=True, timeout=180)
            pages.append(out.stdout)
    txt = "\n".join(pages)
    if cache and url:
        cf = cache / (hashlib.md5(url.encode()).hexdigest() + ".ocr.txt")
        cf.write_text(txt, encoding="utf-8")
    return txt


def norm_name(sur_raw, first_raw):
    """Map OCR 'Nazwisko Imię' row to canonical roster name via surname dictionary."""
    sur = unicodedata.normalize("NFKD", sur_raw).encode("ascii", "ignore").decode().lower()
    if sur in _SUR:
        return _SUR[sur]
    cand = difflib.get_close_matches(sur, list(_SUR.keys()), n=1, cutoff=0.75)
    if cand:
        canon = _SUR[cand[0]]
        # require first-name initial agreement
        if first_raw and canon.split()[0][0].lower() == unicodedata.normalize("NFKD", first_raw).encode("ascii", "ignore").decode()[:1].lower():
            return canon
    return None


_AGG = re.compile(r"ZA[:\s]*(\d+)\s*,?\s*PRZECIW[:\s]*(\d+)\s*,?\s*WSTRZYMUJ[EĘ]\s*SI[EĘ][:\s]*(\d+)\s*,?\s*BRAK\s*G[\ŁL]OSU[:\s]*(\d+)\s*,?\s*NIEOBECNI[:\s]*(\d+)", re.I)
# OCR row prefixes: '1', '1.', '7.', '1 -', 'Il', '11', '12'
_ROW = re.compile(r"(?:^|[\s>\]])(?:\d{1,2}|Il|l)?[.\-]*\s*([A-ZŁŚŹŻĆŃÓĄĘ][\w\-]{2,20})\s+([A-ZŁŚŹŻĆŃÓĄĘ][\w\-]{1,20})\s+(ZA|PRZECIW|WSTRZ\w*(?:\s+SI[EĘ])?|NIEOBECN\w+|BRAK\W*G\w*)(?=[\s\n]|\Z)")
_ROW2 = re.compile(r"^\s*\d{1,2}\s*[Il]\s+([A-ZŁŚŹŻĆŃÓ][\włśźżćńó-]{2,20})\s+([A-ZŁŚŹŻĆŃÓ][\włśźżćńó-]{1,20})\s+(ZA|PRZECIW|WSTRZ\w+|NIEOBECN\w+)\s*$", re.M)


def vote_cat(tok):
    t = tok.upper()
    if t.startswith("ZA"):
        return "za"
    if t.startswith("PRZECIW"):
        return "przeciw"
    if t.startswith("WSTRZ"):
        return "wstrzymal_sie"
    if t.startswith("NIEOBEC"):
        return "nieobecni"
    return None


def parse_columns(seg, decl):
    """Column-OCR layout: 'nazwisko' block, 'imię' block, 'głos' block stacked.
    Valid iff all 15 roster members resolve exactly once with a known vote token."""
    lines = [l.strip() for l in seg.splitlines()]
    try:
        i_naz = next(i for i, l in enumerate(lines) if re.fullmatch(r"nazwisko", l, re.I))
        i_imi = next(i for i, l in enumerate(lines) if i > i_naz and re.fullmatch(r"imi[ęe]", l, re.I))
        i_glos = next(i for i, l in enumerate(lines) if i > i_imi and re.fullmatch(r"g[łl]?os?", l, re.I))
    except StopIteration:
        return None
    def words(i0, i1):
        out = []
        for l in lines[i0 + 1:i1]:
            if not l:
                continue
            out.append(l)
        return out
    surs = [w for w in words(i_naz, i_imi) if re.fullmatch(r"[A-ZŁŚŹŻĆŃÓĄĘ][\w\-]{2,20}", w)]
    firsts = [w for w in words(i_imi, i_glos) if re.fullmatch(r"[A-ZŁŚŹŻĆŃÓĄĘ][\w\-]{1,20}", w)]
    vote_lines = []
    for l in lines[i_glos + 1:]:
        if not l:
            continue
        if re.search(r"Wobec|Przewodniczący|załącznik|uchwała stanowi|Podjęcie|Odby[łl]o", l, re.I):
            break
        vote_lines.append(l)
    if not (len(surs) == len(firsts) == len(vote_lines)) or len(surs) < 14:
        return None
    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
    for sur, first, tok in zip(surs, firsts, vote_lines):
        canon = norm_name(sur, first)
        if not canon:
            return None
        c = vote_cat(tok)
        if not c:
            return None
        named[c].append(canon)
    all_named = named["za"] + named["przeciw"] + named["wstrzymal_sie"] + named["nieobecni"]
    if len(all_named) != len(set(all_named)):
        return None
    return {"za": named["za"], "przeciw": named["przeciw"], "wstrzymal_sie": named["wstrzymal_sie"],
            "nieobecni": named["nieobecni"]}


def parse_votes(full):
    """Split OCR full text into vote blocks; reconcile each vs aggregate."""
    # unify OCR diacritic noise minimally
    text = full.replace("\u00ad", "")
    blocks = []
    for m in re.finditer(r"Wyniki\s*imienne\s*:?", text, re.I):
        start = m.end()
        nxt = re.search(r"Wyniki\s*imienne", text[start:start + 6000], re.I)
        endm = re.search(r"Wobec powyższego|Wobec powyzszego|Protok[óo]ł|Kolejny punkt|===P\d+===", text[start:start + 6000])
        cut = 6000
        if nxt:
            cut = min(cut, nxt.start())
        if endm:
            cut = min(cut, endm.start())
        seg_rows = text[start:start + cut]
        # aggregate: look back from block start up to 1200 chars
        back = text[max(0, m.start() - 1400):m.start()]
        aggs = list(_AGG.finditer(back))
        if not aggs:
            continue
        agg = aggs[-1]
        decl = {"za": int(agg.group(1)), "przeciw": int(agg.group(2)),
                "wstrzymal_sie": int(agg.group(3)), "nieobecni": int(agg.group(5))}
        # topic: text between 'głosowanie nad' and the aggregate
        tm = re.search(r"g[\łl]osowanie nad\s+(.*)$", back, re.S | re.I)
        topic = ""
        if tm:
            topic = tm.group(1)
            topic = re.split(r"Wyniki\s*g[\łl]osowania", topic)[0]
            topic = re.sub(r"\s+", " ", topic).strip().rstrip(".:").replace("\n", " ")
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        rows = _ROW.findall(seg_rows) or _ROW2.findall(seg_rows)
        for sur, first, tok in rows:
            canon = norm_name(sur, first)
            if not canon or canon in named["za"] + named["przeciw"] + named["wstrzymal_sie"] + named["nieobecni"]:
                continue
            c = vote_cat(tok)
            if c:
                named[c].append(canon)
            elif tok.upper().startswith("NIEOBECA"):
                named["nieobecni"].append(canon)
        got = {k: len(named[k]) for k in ("za", "przeciw", "wstrzymal_sie", "nieobecni")}
        ok = all(decl[k] == got[k] for k in decl)
        if not ok:
            col = parse_columns(seg_rows, decl)
            if col:
                got2 = {k: len(col[k]) for k in decl}
                if all(decl[k] == got2[k] for k in decl):
                    named = col
                    got = got2
                    ok = True
        blocks.append({"topic": topic, "named": {k: named[k] for k in ("za", "przeciw", "wstrzymal_sie")},
                       "declared": decl, "got": got, "ok": ok})
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    ids = item_pages(cache)
    print(f"[zbaszyn] protocol items: {len(ids)}")
    records = []
    sess_seen = set()
    n_ok = n_bad = 0
    for iid in ids:
        url, date_iso = item_pdf_url(iid, cache)
        if not url:
            print(f"  {iid}: no pdf")
            continue
        if not date_iso or date_iso < KAD_START:
            print(f"  {iid}: date {date_iso!r} outside IX kad. — skip")
            continue
        raw = fetch(url, binary=True, cache=cache)
        full = ocr_pdf(raw, cache=cache, url=url)
        hm = re.search(r"Protok[óo][łl]\s*Nr?\s*([IVXLCDM]+)\s*/\s*(\d{4})", full)
        snum = hm.group(1) if hm else ""
        votes = parse_votes(full)
        for v in votes:
            if v["ok"]:
                n_ok += 1
            else:
                n_bad += 1
                print(f"     MISMATCH {date_iso}: decl={v['declared']} got={v['got']} topic={v['topic'][:50]}")
        kept = [v for v in votes if v["ok"]]
        print(f"  {iid} {snum} {date_iso}: votes={len(votes)} ok={len(kept)}")
        if date_iso in sess_seen:
            continue
        sess_seen.add(date_iso)
        for v in kept:
            records.append({"session_date": date_iso, "session_num": snum,
                            "topic": v["topic"], "named": v["named"]})
    print(f"[zbaszyn] sessions={len(sess_seen)} votes_ok={n_ok} votes_bad={n_bad}")
    if not records:
        print("[zbaszyn] NO RECONCILED VOTES — aborting")
        return 1

    # ---- build Radoskop output (praszka builders) ----
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec["session_date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["session_num"], "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec["session_num"],
                          "topic": rec["topic"], "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {n: {"name": n, "club": "", "district": None,
                           "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0}
                       for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
                    if cat == "za":
                        councilors_data[nm]["votes_za"] += 1
                    elif cat == "przeciw":
                        councilors_data[nm]["votes_przeciw"] += 1
                    else:
                        councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    csel = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                csel[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(csel.get(c["name"], set())) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": "", "district": None,
                                "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": 0,
                                "votes_nieobecny": 0, "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    ns = sorted(vectors.keys())
    for a, b in combinations(ns, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for v2 in common if vectors[a][v2] == vectors[b][v2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KAD, "label": KAD_LABEL, "clubs": {}, "sessions": sessions_data,
           "total_sessions": total_sessions, "total_votes": total_votes,
           "total_councilors": len(councilors_list), "councilors": councilors_list,
           "votes": all_votes, "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    output = {"generated": datetime.now().isoformat(), "default_kadencja": KAD, "kadencje": [kad]}

    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec["session_date"]
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in rec["named"].get(cat, []):
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    for nm in ROSTER:
        vd = cv.get(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        profiles.append({"name": nm, "slug": slugify(nm),
                         "kadencje": {KAD: {
                             "club": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / max(1, total_sessions) * 100, 1),
                             "aktywnosc": round((vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": 0, "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [],
                             "roles": [ROLES[nm]] if nm in ROLES else [], "notes": "",
                             "former": False, "mid_term": False}}})
    profiles = {"profiles": profiles, "total": len(profiles)}

    out_path = city_dir / "docs" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad2 in output["kadencje"]:
        kid = kad2["id"]
        stubs.append({"id": kid, "label": kad2.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad2, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": output["generated"], "default_kadencja": KAD, "kadencje": stubs},
                  f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[zbaszyn] FINAL: votes={total_votes} sessions={total_sessions} councilors={len(councilors_list)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
