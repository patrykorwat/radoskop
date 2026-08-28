#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Błonie — imienne głosowania Rady Miejskiej w Błoniu (IX kadencja 2024-2029).

Źródło: BIP bip.blonie.pl (CMS /articles/{id}/{slug}), drzewo
  Rada Miejska -> Kadencja 2024-2029 (/articles/330) -> Protokoły z sesji (/articles/341)
  -> lata 2024(/346), 2025(/372), 2026(/396).
Każdy rok listuje per-sesja post "Protokół nr XX/26 z Sesji Rady Miejskiej w Błoniu
odbytej w dniu DD miesiąc RRRR" z załącznikiem PDF protokołu (/downloadFile/{id}).
Protokoły są TEKSTOWE i zawierają imienne głosowania w klasycznym eSesja FORMACIE TEKSTOWYM:
    Głosowanie w sprawie:
    <temat>
    Wyniki głosowania
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n)
    Nazwisko Imię, ...
    PRZECIW (n) / WSTRZYMUJĘ SIĘ (n) / ...
Nazwy w PDF są w kolejności "Nazwisko Imię" — normalizujemy do "Imię Nazwisko" (konwencja
Radoskopa) za pomocą kanonicznego rostera Rady (Skład Rady Miejskiej IX kadencji) i
przypisujemy kluby z "Kluby Radnych 2024-2029" (Forum Samorządowe Gmin, Projekt Błonie).

Sesje od I/24 (2024-05-07) do XXX/26 (2026-06-09) — 31 sesji IX kadencji.

Użycie:
    python scrape_blonie.py --city-dir <cities/blonie> [--work-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.blonie.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
YEAR_ARTICLES = {2024: "/articles/346/2024?m=346",
                 2025: "/articles/372/2025?m=372",
                 2026: "/articles/396/2026?m=396"}

# Kanoniczny skład Rady Miejskiej Błonia IX kadencji (Imię Nazwisko) — z BIP /articles/331
ROSTER = [
    "Tomasz Wiśniewski", "Grzegorz Banaszkiewicz", "Jacek Cieślak",
    "Marzena Cichewicz", "Maciej Górski", "Janusz Guzik",
    "Aleksandra Janas-Malinowska", "Tomasz Janowski", "Marcin Kołota",
    "Grzegorz Kołpaczyński", "Grażyna Laskowska", "Katarzyna Mielcarz",
    "Mirosław Nowakowski", "Maciej Pater", "Aneta Piotrowska",
    "Andrzej Pływaczewski", "Ewa Podyma", "Helena Szymańska",
    "Waldemar Szymański", "Jarosław Uraszewski", "Barbara Wielogórska",
    # zastępca radnego (mid-term 2025) — pojawia się w imiennych listach sesji 2025
    "Jakub Bargieł",
]
CLUB_ASSIGN = {
    "Katarzyna Mielcarz": "Forum Samorządowe Gmin",
    "Jacek Cieślak": "Forum Samorządowe Gmin",
    "Maciej Górski": "Forum Samorządowe Gmin",
    "Marcin Kołota": "Forum Samorządowe Gmin",
    "Marzena Cichewicz": "Projekt Błonie",
    "Mirosław Nowakowski": "Projekt Błonie",
    "Aneta Piotrowska": "Projekt Błonie",
}

_MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
           "sierpnia":8,"wrzesnia":9,"września":9,"pazdziernika":10,"października":10,
           "listopada":11,"grudnia":12}
_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,"XI":11,
          "XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,"XX":20,
          "XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,"XXVIII":28,
          "XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33}

REQ_DELAY = 0.8
_LAST = 0.0
def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir=None):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(5):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                (cache_dir / (key + ".dat")).write_bytes(data)
            return data
        except (ConnectionError, Timeout, OSError) as e:
            if attempt == 4:
                raise
            time.sleep(3 + attempt * 4)
    raise RuntimeError(f"GET failed: {url}")

# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    from html import unescape
    sessions = []
    seen = set()
    for year, path in YEAR_ARTICLES.items():
        t = _get(BIP + path, cache_dir).decode("utf-8", "ignore")
        ts = re.sub(r"\s+", " ", t)
        # split by event-post blocks
        blocks = re.split(r'class="event-post', ts)
        for b in blocks[1:]:
            hm = re.search(r'<h2><a href="[^"]*">([^<]*)</a></h2>', b)
            if not hm:
                continue
            title = hm.group(1).strip()
            dm = re.search(r"w dniu (\d{1,2}) (\w+) (\d{4})", title)
            if not dm:
                continue
            day = int(dm.group(1)); mon = _MONTHS.get(dm.group(2).lower()); yr = int(dm.group(3))
            if not mon:
                continue
            date = f"{yr}-{mon:02d}-{day:02d}"
            if date < KAD_START:
                continue
            num_m = re.search(r"nr\s+(XXX?I?I?|XXX?I?|XXX?|XX?I?I?|XX?I?|XX?|X?I?)/", title, re.I)
            num = None
            rn = re.search(r"nr\s+([IVXLCDM]+)/", title)
            if rn:
                num = _ROMAN.get(rn.group(1))
            # pdf attachment within this block
            pdfs = re.findall(r"downloadFile/(\d+)'[^>]*>\s*<span[^>]*>([^<]*)\.pdf", b)
            pdfid = None
            for pid, fn in pdfs:
                low = fn.strip().lower()
                if low.startswith("protokól") or low.startswith("protokol"):
                    pdfid = pid; break
            if pdfid is None and pdfs:
                pdfid = pdfs[0][0]
            uniq = date
            if uniq in seen:
                continue
            seen.add(uniq)
            sessions.append({"date": date, "num": num, "title": title, "pdfid": pdfid, "year": year})
    sessions.sort(key=lambda s: (s["date"], s["num"] or 0))
    return sessions

# ---------------- eSesja imienne PDF parsing ---------------- (format jak Nakło)
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

# Kanoniczny roster jako kotwica atrybucji: normalizujemy (NFKD strip diakrytów, usuwamy
# znaki niebędące literami/spacjami) i mapujemy BOTH kolejności ("Imię Nazwisko" i
# "Nazwisko Imię") na kanoniczne "Imię Nazwisko". Ekstrakcja bierze TYLKO nazwiska
# pasujące do rostera — narracja/numery stron nie mogą się "przecieknąć" do atrybucji.
def _norm_key(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    lower = s.lower()
    # usuń dywizy (łączy złamane przy zawijaniu "Janas- Malinowska") i znaki nieliterowe
    pieces = re.findall(r"[a-ząćęłńóśźż]+", lower)
    return " ".join(pieces)

_NORM_MAP: dict[str, str] = {}
_KEYS_BY_LEN: list[tuple[str, str]] = []  # (norm_key, canonical)
for __canon in ROSTER:
    __toks = __canon.split()
    __first = " ".join(__toks[:-1]); __last = __toks[-1]
    _ff = _norm_key(f"{__first} {__last}")
    _sf = _norm_key(f"{__last} {__first}")
    for __k in (_ff, _sf):
        if __k not in _NORM_MAP:
            _NORM_MAP[__k] = __canon
for __k, __c in _NORM_MAP.items():
    _KEYS_BY_LEN.append((__k, __c))
_KEYS_BY_LEN.sort(key=lambda x: len(x[0]), reverse=True)

def _extract_names(chunk: str, expected: int) -> list[str]:
    """Wyciągnij dokładnie `expected` nazwisk z chunk-u, kotwicząc w rosterze."""
    norm = _norm_key(chunk)
    if not norm:
        return []
    out = []
    i = 0
    n = len(norm)
    while i < n and len(out) < expected:
        matched = False
        for k, canon in _KEYS_BY_LEN:
            if norm.startswith(k, i):
                out.append(canon)
                i += len(k)
                matched = True
                break
        if not matched:
            i += 1
    return out

def parse_imienne_payload(data):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    if "Wyniki imienne" not in text:
        return []
    records = []
    markers = [m.start() for m in re.finditer(r"Wyniki g\u0142osowania|Wyniki glosowania", text)]
    if not markers:
        return records
    for i, pos in enumerate(markers):
        end = markers[i + 1] if i + 1 < len(markers) else len(text)
        blk = text[pos:end]
        if "Wyniki imienne" not in blk:
            continue
        # temat = tekst między ostatnim "GłosOwanIe/wSprawie" przed tym markerem a markerem
        topic = ""
        seg_before = text[max(0, pos - 2500):pos]
        gs = seg_before.rfind("Głosowanie w sprawie:")
        if gs == -1:
            gs = seg_before.rfind("Głosowano w sprawie:")
        if gs != -1:
            topic = re.sub(r"\s+", " ", seg_before[gs + len("Głosowanie w sprawie:"):])
            topic = topic.rstrip(" .,:;-")
        rec = _parse_block(blk, topic)
        if rec:
            records.append(rec)
    return records

def _parse_block(blk, topic=""):
    cm = _COUNTS_RE.search(blk)
    if not cm:
        return None
    za, przeciw, wstrzym, brak, nieob = (int(x) for x in cm.groups())
    counts = {"za": za, "przeciw": przeciw, "wstrzymal_sie": wstrzym,
              "brak": brak, "nieobecni": nieob}
    topic = (topic or "").strip(" .,:;-\n") or "(glosowanie)"
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
        for cut in ("Głosowanie z dnia", "Głosowanie zakończono", "Wygenerowano",
                    "głosowania z dnia", "Przewodniczący Rady", "Uchwale został nadany",
                    "Uchwale zostal nadany", "Uchwa była", "Zarządził głosowanie",
                    "stwierdził, że", "|"):
            idx = chunk.find(cut)
            if idx != -1:
                chunk = chunk[:idx]
                break
        # nazwiska wyciągamy kotwicząc w kanonicznym rosterze (TYLKO pasujące do rostera,
        # w obu kolejnościach) — narracja i numery stron nie mogą wpłynąć na atrybucję;
        # bierzemy dokładnie `expected` (licznik z nagłówka).
        chunk = re.sub(r"\s+", " ", chunk)
        named[cat] = _extract_names(chunk, expected)
    return {"topic": topic, "counts": counts, "named": dict(named)}

def validate_vote(rec):
    for cat, expected in rec["counts"].items():
        got = len(rec["named"].get(cat, []))
        if got != expected:
            return False, f"{cat}: got {got} expect {expected}"
    return True, ""

# ---------------- output ----------------
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
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    work_dir = Path(args.work_dir) if args.work_dir else city_dir / "work"
    pdf_dir = work_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir) if args.cache_dir else None

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = dict(CLUB_ASSIGN)
    club_assign.update(cfg.get("club_assignments", {}) or {})

    sessions = discover_sessions(cache)
    print(f"[blonie] {len(sessions)} sesji IX kad. (2024-2029)")
    for s in sessions:
        print(f"  sess {s['date']} nr{s['num']} pdf={s['pdfid']}")

    records = []
    for se in sessions:
        if not se["pdfid"]:
            print(f"  [NO-PDF {se['date']}] nr{se['num']}")
            continue
        url = f"{BIP}/downloadFile/{se['pdfid']}"
        pdf_name = f"{se['date']}_nr{se['num'] or '?'}.pdf"
        pdf_path = pdf_dir / pdf_name
        data = _get(url, cache)
        pdf_path.write_bytes(data)
        recs = parse_imienne_payload(data)
        if not recs:
            print(f"  [NO-IMIENNE {se['date']}] nr{se['num']} (pdf {len(data)} B)")
            continue
        tmp = []
        for r in recs:
            ok, msg = validate_vote(r)
            if ok:
                r["date"] = se["date"]; r["num"] = se["num"]
                tmp.append(r)
            else:
                print(f"    [VAL-FAIL {se['date']}] {msg}")
        records += tmp
        print(f"  [ok] {se['date']} nr{se['num']} votes={len(tmp)}")

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
    print(f"[blonie] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
