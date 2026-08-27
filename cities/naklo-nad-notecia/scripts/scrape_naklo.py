#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Nakło nad Notecią — imienne głosowania Rady Miejskiej w Nakle nad Notecią (IX kadencja).

Źródło: BIP bip.gmina-naklo.pl (custom 'artykuly' CMS), kategoria
"Protokoły z sesji" (/artykuly/120/protokoly-z-sesji, paginacja ?page=N).
Dla KAŻDEJ sesji IX kadencji (I…XXVII) publikowany jest artykuł protokołu
(/artykul/120/{id}/protokol-nr-{n}-…-kadencji-2024-2029-{dd}-{mies}-{rrrr}-r) z wieloma
załącznikami PDF (/attachments/download/{id}). Pierwszy załącznik to protokół narracyjny
("Protokół …"), pozostałe to "załącznik nr N". Wiele załączników to "Imienny wykaz głosowania"
w klasycznym eSesja FORMACIE TEKSTOWYM:
    Wyniki głosowania
    Głosowano w sprawie: <temat>
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n)
    <imię nazwisko, …>
    PRZECIW (n) / WSTRZYMUJĘ SIĘ (n) / BRAK GŁOSU (n) / NIEOBECNI (n)

Pobieramy WSZYSTKIE załączniki każdego artykułu IX kadencji, parsujemy te w formacie imiennym
(dyskryminator: marker "Wyniki imienne"), resztę (uchwały tekstowe, skany bez warstwy tekstowej)
pomijamy. Temat + głosy per radny są w samym PDF (nie trzeba parsować protokołu narracyjnego).
Data/nr sesji z URL slug. Skład = pełny zbiór unikalnych nazwisk-kandydatów radnych z głosowań.
Walidacja per głos: zsumowane głosy imienne == agregaty z nagłówka.

Użycie:
    python scrape_naklo.py --city-dir <cities/naklo-nad-notecia> [--work-dir dir] [--cache-dir dir]
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
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.gmina-naklo.pl"
CATEGORY = "/artykuly/120/protokoly-z-sesji"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_MONTHS = {
    "stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
    "sierpnia":8,"wrzesnia":9,"września":9,"pazdziernika":10,"października":10,
    "pazdziernik":10,"październik":10,"listopada":11,"grudnia":12,"grudna":12,
}

REQ_DELAY = 0.8
_LAST = 0.0

def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir):
    """GET bytes; cache to cache_dir/<md5>.dat when provided. Retry z backoff na chwilowe
    błędy połączenia (źródło bip.gmina-naklo.pl potrafi na chwilę odcinać przy serii pobrań)."""
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                (cache_dir / (key + ".dat")).write_bytes(data)
            return data
        except (ConnectionError, Timeout, OSError) as e:
            if attempt == 5:
                raise
            time.sleep(3 + attempt * 4)  # backoff: 3,7,11,15,19s
    raise RuntimeError(f"GET failed: {url}")

# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """Paginate the category, collect IX-kadencja protocol article URLs + num/date."""
    from html import unescape
    sessions = []
    seen = set()
    page = 1
    while True:
        url = BIP + CATEGORY if page == 1 else f"{BIP}{CATEGORY}?page={page}"
        t = _get(url, cache_dir).decode("utf-8", "ignore")
        hrefs = []
        for m in re.finditer(r'href="([^"]*artykul/120/\d+/[^"]+)"', t):
            href = unescape(m.group(1))
            if "artykul/120/" in href and href not in seen:
                seen.add(href); hrefs.append(href)
        if not hrefs:
            break
        found_ix = False
        for href in hrefs:
            if "kadencji-2024-2029" not in href:
                continue
            found_ix = True
            num = None
            nm = re.search(r'protokol-nr-(\d+)-(\d{4})', href)
            if nm:
                num = int(nm.group(1))
            dm = None
            for m in re.finditer(r'-(\d{1,2})-([a-ząćęłńóśźż]+)-(\d{4})-r\b', href):
                dm = m  # keep LAST
            date = None
            if dm:
                day = int(dm.group(1)); mon = _MONTHS.get(dm.group(2)); year = int(dm.group(3))
                if mon:
                    date = f"{year}-{mon:02d}-{day:02d}"
            if not date or date < KAD_START:
                continue
            sessions.append({"url": href, "date": date, "num": num})
        if not found_ix:
            break
        page += 1
        if page > 5:
            break
    sessions.sort(key=lambda s: (s["date"], s["num"] or 0))
    return sessions

def article_attachments(article_url, cache_dir):
    """Return ordered [(title, href)] of all /attachments/download/{id} links in an article."""
    from html import unescape
    t = _get(article_url, cache_dir).decode("utf-8", "ignore")
    out = []
    for m in re.finditer(r'<a[^>]*href="([^"]*attachments/download/(\d+))"[^>]*>(.*?)</a>', t, re.S):
        href = unescape(m.group(1))
        title = re.sub(r"<[^>]+>", " ", m.group(3))
        title = re.sub(r"\s+", " ", title).strip()
        out.append((title, href))
    return out

# ---------------- eSesja imienne PDF parsing ----------------
_LABEL_RE = re.compile(r'\b(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\((\d+)\)')
_COUNTS_RE = re.compile(
    r'ZA:\s*(\d+),?\s*PRZECIW:\s*(\d+),?\s*WSTRZYMUJĘ SIĘ:\s*(\d+),?\s*'
    r'BRAK GŁOSU:\s*(\d+),?\s*NIEOBECNI:\s*(\d+)')

_FOOTER_TOKENS = re.compile(
    r'(zakończono|godz|wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|'
    r'głosowanie\s*z\s*dnia|w\s*dniu:|\d{1,2}:\d{2}:\d{2}|\|)', re.I)

def _clean_name(s):
    s = s.strip()
    if not s:
        return None
    if not any(c.isalpha() for c in s):
        return None
    if _FOOTER_TOKENS.search(s):
        return None
    return re.sub(r"\s+", " ", s)

def parse_imienne_payload(data):
    """Parse a PDF payload; return list of vote records or [] if not eSesja-imienne format."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    if "Wyniki imienne" not in text:
        return []
    records = []
    # each vote block begins with 'Wyniki glosowania'/'Wyniki głosowania'
    blocks = re.split(r'(?=Wyniki g\u0142osowania|Wyniki glosowania)', text)
    for blk in blocks:
        if "Wyniki imienne" not in blk:
            continue
        rec = _parse_block(blk)
        if rec:
            records.append(rec)
    return records

def _parse_block(blk):
    cm = _COUNTS_RE.search(blk)
    if not cm:
        return None
    za, przeciw, wstrzym, brak, nieob = (int(x) for x in cm.groups())
    counts = {"za": za, "przeciw": przeciw, "wstrzymal_sie": wstrzym,
              "brak": brak, "nieobecni": nieob}

    # topic = text between 'Głosowano w sprawie:' and the counts line
    gs = blk.find("Głosowano w sprawie:")
    if gs == -1:
        return None
    topic_raw = blk[gs + len("Głosowano w sprawie:"):cm.start()]
    topic = re.sub(r"\s+", " ", topic_raw).strip(" .,:;-")
    topic = topic or "(glosowanie)"

    # named sections after 'Wyniki imienne:'
    wi = blk.find("Wyniki imienne")
    remainder = blk[wi:]
    labels = list(_LABEL_RE.finditer(remainder))
    named = defaultdict(list)
    for i, m in enumerate(labels):
        cat_map = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
                   "BRAK GŁOSU": "brak", "NIEOBECNI": "nieobecni"}
        cat = cat_map.get(m.group(1)); expected = int(m.group(2))
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
        chunk = remainder[start:end]
        # names are comma-separated; a name may wrap across lines WITHOUT a comma,
        # so collapse all whitespace to a single space BEFORE splitting on commas,
        # otherwise "Daniel<newline>Kończak" becomes two bogus tokens.
        # Also cut off the trailing footer (last section only) before splitting.
        for cut in ("Głosowanie z dnia", "Głosowanie zakończono", "Wygenerowano",
                    "głosowania z dnia", "|"):
            idx = chunk.find(cut)
            if idx != -1:
                chunk = chunk[:idx]
                break
        chunk = re.sub(r"\s+", " ", chunk)
        tokens = [t for t in (_clean_name(x) for x in chunk.split(",")) if t]
        named[cat] = tokens
    return {"topic": topic, "counts": counts, "named": dict(named)}

def validate_vote(rec):
    """Check per-category parsed counts == header counts."""
    for cat, expected in rec["counts"].items():
        got = len(rec["named"].get(cat, []))
        if got != expected:
            return False, f"{cat}: got {got} expect {expected}"
    return True, ""

# ---------------- output (wzorowane na goleniow) ----------------
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak"):
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
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "nieobecni":
                    councilors_data[nm]["votes_nieobecny"] += 1
                elif cat == "brak":
                    councilors_data[nm]["votes_brak"] += 1
                elif cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0,
                              "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True, help="target cities/<slug> dir (writes docs/)")
    ap.add_argument("--work-dir", default=None, help="workspace dir for pdfs cache")
    ap.add_argument("--cache-dir", default=None, help="dir to cache raw HTTP responses")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    work_dir = Path(args.work_dir) if args.work_dir else city_dir / "work"
    pdf_dir = work_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir) if args.cache_dir else None

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[naklo] {len(sessions)} sesji IX kad. (2024-2029)")

    records = []
    for se in sessions:
        n_votes = 0; n_skip = 0
        try:
            for title, href in article_attachments(se["url"], cache):
                if "kadencji-2024-2029" not in se["url"]:
                    continue
                pdf_name = re.sub(r"[^A-Za-z0-9]+", "_", href).strip("_") + ".pdf"
                pdf_path = pdf_dir / pdf_name
                data = _get(href, cache)  # cache raw bytes; also persist pdf under pdfs/
                pdf_path.write_bytes(data)
                recs = parse_imienne_payload(data)
                if not recs:
                    n_skip += 1
                    continue
                tmp = []
                for r in recs:
                    ok, msg = validate_vote(r)
                    if ok:
                        r["date"] = se["date"]; r["num"] = se["num"]
                        tmp.append(r)
                    else:
                        print(f"    [VAL-FAIL {se['date']}] {msg}")
                records += tmp; n_votes += len(tmp)
            print(f"  [ok] {se['date']} nr{se['num'] or '?'} votes={n_votes} skip={n_skip}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[naklo] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
