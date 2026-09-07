#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Pobiedziska — imienne głosowania Rady Miejskiej Gminy Pobiedziska (IX kadencja 2024-2029).

Źródło: BIP bip.pobiedziska.pl — Madkom React-SPA z JSON API na tej samej domenie:
  GET /api/menu/{menuId}/articles?limit=..&offset=..   -> lista artykułów kategorii
  GET /api/articles/{articleId}                        -> artykuł z attachments[]
Kategoria "Protokoły" = menuId 55 (Rada Gminy -> Protokoły). Per-sesja artykuł
"Protokół <RZYMSKA> [Nadzwyczajnej] sesji ... z dnia D miesiąc RRRR" z załącznikiem
PDF protokołu: https://bip.pobiedziska.pl/e,pobierz,get.html?id={attachmentId}
(pobierany przez /api/files/{id}).

Protokoły są TEKSTOWE z głosowaniami imiennymi w klasycznym eSesja FORMACIE TEKSTOWYM:
    Głosowano w sprawie:
    <temat>
    Wyniki głosowania
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ : n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n)
    Imię Nazwisko, ...   (nazwiska w kolejności IMIĘ NAZWISKO, zgodnej z rostrem)

Skład (15 radnych) kuratorowany z BIP "Skład Rady" (artykuł 15259, IX kadencja).
eSesja pobiedziska.esesja.pl = Portal Mieszkańca PM-B (pusta sessions-list) — bez danych.

Użycie:
    python scrape_pobiedziska.py --city-dir <cities/pobiedziska> [--work-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import html as _html
import io
import json
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber

BIP = "https://bip.pobiedziska.pl"
MENU_PROTOKOLY = 55
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Radoskop/1.0 (info@radoskop.eu)"}

# Kanoniczny skład Rady Miejskiej Gminy Pobiedziska IX kadencji (Imię Nazwisko)
# — z BIP "Skład Rady" (artykuł 15259, stan 2026-04).
ROSTER = [
    "Józef Czerniawski",   # Przewodniczący Rady
    "Barbara Widelicka",   # Wiceprzewodnicząca
    "Ewa Tabaczyńska",     # Wiceprzewodnicząca
    "Paweł Bzdurski",
    "Piotr Horbik",
    "Andrzej Jackowiak",
    "Grażyna Kędziora",
    "Sara Kęsicka",
    "Magdalena Klorek",
    "Witold Meller",
    "Katarzyna Paczka",
    "Michał Nowak",
    "Piotr Raczek",
    "Wojciech Radecki",
    "Piotr Wala",
]
CLUB_ASSIGN: dict[str, str] = {}  # PENDING — kuratorować z BIP, nie fabrykować

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9, "pazdziernika": 10,
           "października": 10, "listopada": 11, "grudnia": 12}
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
          "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16,
          "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23,
          "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29,
          "XXX": 30, "XXXI": 31, "XXXII": 32, "XXXIII": 33, "XXXIV": 34, "XXXV": 35,
          "XXXVI": 36, "XXXVII": 37, "XXXVIII": 38, "XXXIX": 39, "XL": 40, "XLI": 41,
          "XLII": 42, "XLIII": 43, "XLIV": 44, "XLV": 45, "XLVI": 46, "XLVII": 47,
          "XLVIII": 48, "XLIX": 49, "L": 50}

_COUNTS_RE = re.compile(
    r"ZA\s*:\s*(\d+)\s*,\s*PRZECIW\s*:\s*(\d+)\s*,\s*WSTRZYMUJ[ĘE] SI[ĘE]\s*:\s*(\d+)\s*,"
    r"\s*BRAK G\u0141OSU\s*:\s*(\d+)\s*,\s*NIEOBECNI\s*:\s*(\d+)", re.I)
_LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJ[ĘE] SI[ĘE]|BRAK G\u0141OSU|NIEOBECNI)\s*[\(\:]\s*(\d+)")
_FOOTER_TOKENS = re.compile(
    r"strona\s*\d|podstrona|id=\d|pdf|generowan|protokol|protok\xf3\u0142\s*\d|druk\s*\d"
    r"|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|\d+\s*/\s*\d+|bip\.pobiedziska"
    r"|głosowanie\s*z\s*dnia|w\s*dniu:|\|", re.I)


def _norm_key(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    lower = s.lower()
    pieces = re.findall(r"[a-ząćęłńóśźż]+", lower)
    return " ".join(pieces)


_NORM_MAP: dict[str, str] = {}
for __canon in ROSTER:
    __toks = __canon.split()
    __first = " ".join(__toks[:-1]); __last = __toks[-1]
    for __k in (_norm_key(f"{__first} {__last}"), _norm_key(f"{__last} {__first}")):
        if __k not in _NORM_MAP:
            _NORM_MAP[__k] = __canon
# literówki w źródłowych PDF-ach (obserwowane): "Barbara Wdelicka" (brak 'i')
_NORM_MAP[_norm_key("Barbara Wdelicka")] = "Barbara Widelicka"
_KEYS_BY_LEN = sorted(_NORM_MAP.items(), key=lambda x: len(x[0]), reverse=True)


def _extract_names(chunk: str, expected: int) -> list[str]:
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


def _get(url: str, cache_dir=None, binary=True):
    cp = None
    if cache_dir:
        import hashlib
        cp = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest()
                                + (".bin" if binary else ".txt"))
        if cp.is_file():
            return cp.read_bytes() if binary else cp.read_text(encoding="utf-8").encode()
    req = urllib.request.Request(url, headers=UA)
    data = urllib.request.urlopen(req, timeout=30, context=CTX).read()
    if cp is not None:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(data)
    time.sleep(0.4)
    return data


def get_json(path: str, cache_dir=None):
    return json.loads(_get(BIP + path, cache_dir, binary=False).decode("utf-8"))


def _session_date(title: str):
    """Zwróć ISO date z tytułu ('z dnia 16 lipca 2026r.', literówki '20206r' tolerowane
    przez odczyt 4 cytat od końca roku), lub None."""
    m = re.search(r"z\s*dnia\s+(\d{1,2})\s+([a-ząęłńóśźż]+)\.?\s+(\d{4,5})r?", title, re.I)
    if not m:
        m = re.search(r"\b(\d{1,2})\s+([a-ząęłńóśźż]+)\.?\s+(\d{4,5})r?\b", title, re.I)
    if not m:
        return None
    day, mon_s, year_s = m.group(1), m.group(2).lower(), m.group(3)
    if len(year_s) == 5:
        # literówka w tytule BIP (np. "20206r") — usuń jeden znak, weź rok w [2023,2028]
        cands = [year_s[:i] + year_s[i + 1:] for i in range(5)]
        year_s = next((c for c in cands if 2023 <= int(c) <= 2028), year_s[:4])
    mon = _MONTHS.get(mon_s.rstrip("."))
    if mon is None:
        return None
    try:
        return datetime(int(year_s), mon, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _session_num(title: str) -> str:
    m = re.search(r"Protok[óo][lł]\s+([IVXL]+)", title, re.I)
    return m.group(1).upper() if m else ""


def discover_sessions(cache_dir=None):
    """Artykuły kategorii Protokoły (menuId 55) -> sesje IX kadencji z PDF-em."""
    sessions = []
    seen = set()
    offset = 0
    while True:
        d = get_json(f"/api/menu/{MENU_PROTOKOLY}/articles?limit=50&offset={offset}", cache_dir)
        arts = d.get("articles", [])
        new = 0
        for a in arts:
            aid = a["id"]
            if aid in seen:
                continue
            new += 1
            seen.add(aid)
            title = _html.unescape(re.sub(r"<[^>]+>", "", a["aliasFields"][0]["value"]))
            title = re.sub(r"\s+", " ", title).strip()
            if "protok" not in title.lower() or "sesj" not in title.lower():
                continue
            date = _session_date(title)
            if not date or date < KAD_START:
                continue
            sessions.append({"artid": aid, "date": date, "num": _session_num(title),
                             "title": title})
        total = int(d.get("total") or 0)
        offset += 50
        if new == 0 or offset >= total:
            break
    sessions.sort(key=lambda s: s["date"])
    # dedupe po dacie (ten sam dzień = jedna sesja, weź pierwszy artykuł z PDF-em później)
    return sessions


def attachment_pdf(artid: str, cache_dir=None):
    art = get_json(f"/api/articles/{artid}", cache_dir)
    for att in art.get("attachments", []):
        if (att.get("extension") or "").lower() == "pdf" and not att.get("deleted"):
            url = f"{BIP}/e,pobierz,get.html?id={att['id']}"
            return url, att.get("name", "")
    return None, None


def parse_imienne_payload(data: bytes):
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
        topic = ""
        seg_before = text[max(0, pos - 2500):pos]
        gsm = None
        for m in re.finditer(r"G\u0142osowan(?:o|ie) w sprawie:", seg_before):
            gsm = m
        if gsm is not None:
            topic = re.sub(r"\s+", " ", seg_before[gsm.end():])
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
        lab = m.group(1).upper()
        if "WSTRZYMUJ" in lab:
            cat = "wstrzymal_sie"
        elif "BRAK" in lab:
            cat = "brak"
        elif "PRZECIW" in lab:
            cat = "przeciw"
        elif lab.startswith("ZA"):
            cat = "za"
        elif "NIEOBECNI" in lab:
            cat = "nieobecni"
        else:
            continue
        expected = int(m.group(2))
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
        chunk = remainder[start:end]
        for cut in ("Głosowanie z dnia", "Głosowanie zakończono", "Wygenerowano",
                    "głosowania z dnia", "Przewodniczący Rady", "Uchwale został nadany",
                    "Uchwale zostal nadany", "Zarządził głosowanie", "Protokół",
                    "stwierdził, że", "|"):
            idx = chunk.find(cut)
            if idx != -1:
                chunk = chunk[:idx]
                break
        chunk = re.sub(r"\s+", " ", chunk)
        named[cat] = _extract_names(chunk, expected)
    return {"topic": topic, "counts": counts, "named": dict(named)}


def validate_vote(rec):
    for cat, expected in rec["counts"].items():
        got = len(rec["named"].get(cat, []))
        if got != expected:
            return False, f"{cat}: got {got} expect {expected}"
    return True, ""


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z',
            'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S',
            'Ź': 'Z', 'Ż': 'Z'}
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
    councilors_data = {}
    for name in ROSTER:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"),
                                 "district": None, "votes_za": 0, "votes_przeciw": 0,
                                 "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0,
                                 "rebellions": []}
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
    all_names = set(cv.keys()) | set(ROSTER)
    for nm in sorted(all_names):
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
                "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


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
    print(f"[pobiedziska] {len(sessions)} sesji IX kad. (>= {KAD_START})")
    for s in sessions:
        print(f"  sess {s['date']} nr{s['num']} art={s['artid']}")

    records = []
    for se in sessions:
        url, name = attachment_pdf(se["artid"], cache)
        if not url:
            print(f"  [NO-PDF {se['date']}] nr{se['num']}")
            continue
        data = _get(url, cache)
        (pdf_dir / f"{se['date']}_nr{se['num'] or '?'}.pdf").write_bytes(data)
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
    print(f"[pobiedziska] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
