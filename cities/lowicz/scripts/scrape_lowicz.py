#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Łowicz — imienne głosowania Rady Miejskiej w Łowiczu (IX kadencja 2024-2029).

Źródło: BIP www.lowicz.eu/bip (custom CMS lowicz.eu). Kategoria "Sesje Rady Miejskiej"
(/Sesje_Rady_Miejskiej,12) publikuje artykuły per sesja (href "{ROMAN},12,{id}")
z załącznikiem "Wykaz głosowań (PDF)" = wydruk eSesja: per głosowanie
"Wyniki imienne / lp / nazwisko / imię / głos" + linie ZA: n, PRZECIW: n, WSTRZYMAŁO SIĘ: n.

Sesje mają ID sekwencyjne (np. XXXIII=382 … XLI=392); listing renderuje tylko 10
najnowszych, więc skaner schodzi w dół po ID (i próbuje w górę) aż za Kadencję IX.

Użycie:
    python scrape_lowicz.py --city-dir <cities/lowicz> [--cache-dir dir]
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
from collections import Counter
from datetime import datetime
from pathlib import Path

import pymupdf
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.lowicz.eu"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Radoskop/1.0"}
REQ_DELAY = 0.4
_LAST = 0.0

_MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
           "sierpnia":8,"września":9,"wrzesnia":9,"października":10,"pazdziernika":10,
           "listopada":11,"grudnia":12}

_ROM_RE = r'(?P<rom>[IVXLCDM]+)'
_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,"XI":11,"XII":12,
        "XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,"XX":20,"XXI":21,
        "XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,"XXVIII":28,"XXIX":29,
        "XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,"XXXV":35,"XXXVI":36,"XXXVII":37,
        "XXXVIII":38,"XXXIX":39,"XL":40,"XLI":41,"XLII":42,"XLIII":43,"XLIV":44,"XLV":45,
        "XLVI":46,"XLVII":47,"XLVIII":48,"XLIX":49,"L":50}


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None, binary=False):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir); cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + (".bin" if binary else ".html"))
        if cf.is_file() and time.time() - cf.stat().st_mtime < 6 * 3600:
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers=HEADERS, timeout=60, verify=False)
    r.raise_for_status()
    if cache_dir:
        (Path(cache_dir) / (key + (".bin" if binary else ".html"))).write_bytes(r.content)
    return r.content


def _decode(b):
    m = re.search(rb'charset=["\']?([\w-]+)', b[:3000], re.I)
    enc = m.group(1).decode("ascii", "ignore").lower() if m else None
    for e in (enc, "utf-8", "cp1250", "windows-1250"):
        if not e:
            continue
        try:
            return b.decode(e)
        except Exception:
            continue
    return b.decode("latin1", "replace")


def parse_session_page(html):
    """Return dict(date, num, title, files=[(label,url)]) or None if not a session page."""
    m = re.search(r'([IVXLCDM]+)(?:\s+nadzwyczajna)?\s+sesja Rady Miejskiej(?: w Łowiczu)?\s*(?:-\s*)?z dnia\s*(\d{1,2})\s+(\w+)\s+(\d{4})', html)
    if not m:
        return None
    rom, day, mon, year = m.group(1), int(m.group(2)), m.group(3).lower(), int(m.group(4))
    monn = _MONTHS.get(mon)
    if not monn:
        return None
    date = f"{year}-{monn:02d}-{day:02d}"
    files = []
    for fm in re.finditer(r'href="([^"]*download\.php\?id=\d+)[^"]*"[^>]*>([^<]{0,120})', html):
        label = re.sub(r"&nbsp;", " ", fm.group(2)).strip()
        url = fm.group(1).replace("&amp;", "&")
        # resolve relative-to-BIP urls: page lives at /bip/<x>; href '../pl/download.php'
        # means /pl/download.php (one level up from /bip/).
        ups = 0
        while url.startswith("../"):
            url = url[3:]
            ups += 1
        if url.startswith("./"):
            url = url[2:]
        if url.startswith("/"):
            url = BASE + url
        elif not url.startswith("http"):
            url = BASE + ("/" if ups else "/bip/") + url
        if "download.php?id=" in url and "path=" not in url:
            url += "&path=../files/docs/"
        files.append((label, url))
    title = re.sub(r"<[^>]+>", "", m.group(0)).strip()
    return {"date": date, "num": _ROM.get(rom, ""), "roman": rom, "title": title, "files": files}


def _int_to_rom(n):
    vals=[(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),
          (10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    s=""
    for v,sym in vals:
        while n>=v:
            s+=sym; n-=v
    return s


def discover_sessions(cache_dir, log):
    """Walk downward from the listing (10 newest) by article-id steps of 1-3,
    guessing the roman numeral (session number) for each id. Returns IX sessions."""
    lst = _decode(_get(BASE + "/Sesje_Rady_Miejskiej,12", cache_dir))
    pairs = re.findall(r'href="([IVXLCDM]+),12,(\d+)"', lst)
    known = {}  # sid -> session dict
    newest_sid = 0
    newest_num = 0
    for rom, sid in pairs:
        sid = int(sid)
        try:
            html = _decode(_get(f"{BASE}/bip/{rom},12,{sid}", cache_dir))
        except Exception:
            continue
        s = parse_session_page(html)
        if s:
            s["sid"] = sid
            known[sid] = s
            num = _ROM.get(rom, 0)
            if sid > newest_sid:
                newest_sid, newest_num = sid, num
    # probe forward (newer sessions above the listing)
    sid, num = newest_sid + 1, newest_num + 1
    while sid <= newest_sid + 8:
        got = None
        for d in (-1, 0, 1):
            rom = _int_to_rom(max(1, num + d))
            try:
                html = _decode(_get(f"{BASE}/bip/{rom},12,{sid}", cache_dir))
            except Exception:
                continue
            s = parse_session_page(html)
            if s:
                got = s
                break
        if got:
            got["sid"] = sid
            known[sid] = got
            num = _ROM.get(got["roman"], num) + 1
            sid += 1
        else:
            sid += 1
    # walk downward from oldest listed
    oldest = min(known) if known else 0
    oldest_num = _ROM.get(known[oldest]["roman"], 0) if oldest else 0
    sid, num = oldest - 1, oldest_num - 1
    miss = 0
    while num >= 1 and miss < 6:
        got = None
        hit_id = None
        for cand in (sid, sid - 1, sid - 2, sid - 3):
            for d in (0, -1):
                rom = _int_to_rom(max(1, num + d))
                try:
                    html = _decode(_get(f"{BASE}/bip/{rom},12,{cand}", cache_dir))
                except Exception:
                    continue
                s = parse_session_page(html)
                if s:
                    got, hit_id = s, cand
                    break
            if got:
                break
        if got:
            miss = 0
            got["sid"] = hit_id
            known[hit_id] = got
            num = _ROM.get(got["roman"], num) - 1
            sid = hit_id - 1
            if got["date"] < "2023-06-01":
                break
        else:
            miss += 1
            sid -= 1
            num -= 1
    out = sorted((s for s in known.values() if s["date"] >= KAD_START), key=lambda x: x["date"])
    log(f"  discovered {len(out)} IX-kadencja sessions")
    return out


# ---------------- PDF parsing (eSesja "Wykaz głosowań" print, TEXT layout) ----------------
_VOTE_LINE = re.compile(r'^(ZA|PRZECIW|WSTRZYMA[LŁ]O? SI[EĘ])$')
_ROSTER_ROW = re.compile(r'^(\d{1,2})\n(.+?)\n(.+?)\n(ZA|PRZECIW|WSTRZYMA\w* SI[EĘ])$', re.M)


def parse_wykaz_pdf(data):
    """Parse eSesja 'Wykaz głosowań' text PDF -> list of vote dicts."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    # split into vote blocks by "N.M. Głosowanie ..." header
    parts = re.split(r'(?m)^(\d{1,2}\.\d{1,2})\.\s+Głosowanie\s+', text)
    votes = []
    # parts: [pre, num1, body1, num2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        vnum = parts[i]
        body = parts[i + 1]
        tm = re.search(r'^([^\n]+(?:\n[^\n]+)*?)\ngłosowanie\n', body, re.S)
        topic = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
        topic = re.sub(r'^projekt\s+', '', topic)
        agg = {}
        am = re.search(r'ZA\n(\d+)\n', body); agg["za"] = int(am.group(1)) if am else None
        am = re.search(r'PRZECIW\n(\d+)\n', body); agg["przeciw"] = int(am.group(1)) if am else None
        am = re.search(r'WSTRZYMAŁO SIĘ\n(\d+)\n', body); agg["wstrzymal_sie"] = int(am.group(1)) if am else None
        wm = re.search(r'Głosowanie zakończone wynikiem: (\w+)', body)
        result = wm.group(1) if wm else ""
        rollcall = []
        # roll-call lives strictly after the 'Wyniki imienne' header of this block
        wi = body.find("Wyniki imienne")
        rc_body = body[wi:] if wi >= 0 else ""
        for rm in _ROSTER_ROW.finditer(rc_body + "\n"):
            lp, surname, given, vote = rm.group(1), rm.group(2).strip(), rm.group(3).strip(), rm.group(4)
            if not surname or not given or "%" in surname or "%" in given:
                continue
            vk = "za" if vote == "ZA" else ("przeciw" if vote == "PRZECIW" else "wstrzymal_sie")
            rollcall.append((f"{given} {surname}", vk))
        if not rollcall:
            continue
        votes.append({"num": vnum, "topic": topic, "agg": agg, "result": result, "rollcall": rollcall})
    return votes


def validate_vote(v, log):
    agg = v["agg"]
    c = Counter(vk for _, vk in v["rollcall"])
    ok = True
    for key in ("za", "przeciw", "wstrzymal_sie"):
        want = agg.get(key)
        got = c.get(key, 0)
        if want is not None and want != got:
            ok = False
    if not ok:
        log(f"    ! aggregate mismatch vote {v['num']}: agg={agg} parsed={dict(c)}")
    return ok


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    out_dir = city_dir / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(*a):
        print(*a, flush=True)

    sessions = discover_sessions(args.cache_dir, log)
    if not sessions:
        log("NO SESSIONS FOUND")
        return 1

    all_votes = []
    roster = Counter()
    session_rows = []
    for s in sessions:
        wykaz = [(lab, u) for lab, u in s["files"] if "imienn" in lab.lower() or "wykaz g" in lab.lower() or "wykaz głosowa" in lab.lower()]
        if not wykaz:
            log(f"  {s['date']} {s['roman']}: brak Wykazu głosowań (pliki: {[l for l,_ in s['files']]})")
            session_rows.append({"date": s["date"], "num": s["roman"], "title": s["title"],
                                 "url": f"{BASE}/bip/{s['roman']},12,{s['sid']}", "vote_count": 0})
            continue
        label, url = wykaz[0]
        try:
            data = _get(url, args.cache_dir, binary=True)
        except Exception as e:
            log(f"  {s['date']} {s['roman']}: PDF download fail {e}")
            continue
        votes = parse_wykaz_pdf(data)
        good = [v for v in votes if validate_vote(v, log)]
        log(f"  {s['date']} {s['roman']}: {len(votes)} votes ({len(good)} validated)")
        for v in good:
            c = Counter(vk for _, vk in v["rollcall"])
            for nm, vk in v["rollcall"]:
                roster[nm] += 1
            all_votes.append({
                "id": f"{s['sid']}-{v['num'].replace('.','-')}",
                "date": s["date"],
                "session": s["roman"],
                "title": v["topic"] or f"Głosowanie {v['num']}",
                "source_url": url,
                "category": "inne",
                "stage": "przeglosowane",
                "result": v["result"],
                "named_votes": {
                    "za": [nm for nm, vk in v["rollcall"] if vk == "za"],
                    "przeciw": [nm for nm, vk in v["rollcall"] if vk == "przeciw"],
                    "wstrzymal_sie": [nm for nm, vk in v["rollcall"] if vk == "wstrzymal_sie"],
                },
            })
        session_rows.append({"date": s["date"], "num": s["roman"], "title": s["title"],
                             "url": f"{BASE}/bip/{s['roman']},12,{s['sid']}", "vote_count": len(good)})

    if not all_votes:
        log("NO VALIDATED VOTES")
        return 1

    councilors = sorted(roster)
    councilor_index = councilors
    votes_out = []
    for v in all_votes:
        votes_out.append({
            "id": v["id"], "date": v["date"], "title": v["title"],
            "session": v["session"], "source_url": v["source_url"],
            "category": v["category"], "stage": v["stage"],
            "named_votes_idx": {
                "za": [councilor_index.index(n) for n in v["named_votes"]["za"] if n in councilor_index],
                "przeciw": [councilor_index.index(n) for n in v["named_votes"]["przeciw"] if n in councilor_index],
                "wstrzymal_sie": [councilor_index.index(n) for n in v["named_votes"]["wstrzymal_sie"] if n in councilor_index],
            },
            "named_votes": v["named_votes"],
        })

    # profiles with stats
    per_name_votes = Counter()
    per_name_za = Counter()
    for v in all_votes:
        for k, names in v["named_votes"].items():
            for nm in names:
                per_name_votes[nm] += 1
                if k == "za":
                    per_name_za[nm] += 1
    total_votes = len(all_votes)
    profiles = {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": [], "total": len(councilors)}
    for nm in councilors:
        n = per_name_votes.get(nm, 0)
        profiles["profiles"].append({
            "name": nm, "slug": slugify(nm), "club": "", "role": "", "photo_url": "",
            "bio": "", "email": "", "social_links": {},
            "voting": {"total": n},
            "kadencje": {
                KADENCJA_ID: {
                    "club": "", "has_voting_data": True, "role": "",
                    "frekwencja": round(100.0 * n / total_votes, 1) if total_votes else 0,
                    "aktywnosc": round(100.0 * n / total_votes, 1) if total_votes else 0,
                    "zgodnosc_z_klubem": None,
                    "votes_total": n,
                }
            },
        })

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "sessions": session_rows,
        "votes": votes_out,
        "councilor_index": councilor_index,
        "councilors": [{"name": nm, "slug": slugify(nm), "club": ""} for nm in councilors],
        "total_councilors": len(councilors),
        "total_votes": total_votes,
        "similarity_top": [], "similarity_bottom": [],
    }
    (out_dir / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    data = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "city": "Łowicz",
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "stats": {"sessions": len(session_rows), "votes": total_votes, "councilors": len(councilors)},
    }
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    # patch config has_voting_data
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["has_voting_data"] = True
    cfg["councilor_count"] = len(councilors)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"DONE: {len(session_rows)} sessions, {total_votes} votes, {len(councilors)} councilors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
