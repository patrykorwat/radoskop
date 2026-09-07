#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Dąbrowa Tarnowska — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.malopolska.pl (platforma "Małopolska BIP", Angular SPA + JSON API),
jednostka umdabrowatarnowska (unit id 26). Kategoria "Rada > Uchwały" (menu 130213) z
podkategorią "Sesje" (menu 130847) → roczniki: 2026 (menu 469464), 2025 (450152), 2024 (429066).
Każdy artykuł = jedna uchwała; kolumna "Numer" = numer aktu, tytuł "UCHWAŁA NR <SESJA>/<AKT>/<ROK>".
Załącznik "wyniki_glosowania(_|-)N" (PDF, WARSTWA TEKSTOWA, wydruk eSesja, format TEXT):

    Wyniki głosowania
    Głosowano w sprawie: <temat> (druk nr <SESJA>/<n>/<rok>),
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n)
    <nazwiska po przecinkach, zawijane wierszami>
    NIEOBECNI (n)
    ...
    Głosowanie zakończono w dniu: DD miesiąc RRRR, o godz. HH:MM
    Wygenerowano w systemie eSesja.pl | ...

Parser bloków imiennych + walidacja: liczba nazwisk per kategoria == licznik w nagłówku
sekcji (ZA (21) -> 21 nazwisk) ORAZ == agregat z linii ZA:/PRZECIW:/...
Data sesji = "Głosowanie zakończono w dniu" z PDF. Numer sesji = rzymski z numeru uchwały.
Roster + kluby wyborcze: kategoria "Rada > Skład Rady > 2024 - 2029" (menu 435729, kolumny
Imię i Nazwisko / Stanowisko / Klub).

Użycie:
    python scrape_dabrowa_tarnowska.py --output docs/data.json --profiles docs/profiles.json \
        [--cache-dir DIR]
"""
import argparse
import hashlib
import html
import io
import json
import re
import ssl
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber

API = "https://bip.malopolska.pl"
YEAR_MENUS = {"2024": "429066", "2025": "450152", "2026": "469464"}
ROSTER_MENU = "435729"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Radoskop/1.0 (info@radoskop.eu)",
      "Accept-Language": "pl,en"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
REQ_DELAY = 0.45
_LAST = 0.0

_COUNTS_RE = re.compile(
    r'ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJ[ĘE]\s*SI[ĘE]:\s*(\d+),\s*BRAK\s*G\u0141OSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)',
    re.I)
_LABEL_RE = re.compile(r'^(ZA|PRZECIW|WSTRZYMUJ[ĘE] SI[ĘE]|BRAK G\u0141OSU|NIEOBECNI)\s*\((\d+)\)\s*$', re.M)
_END_DATE_RE = re.compile(r'G\u0142osowanie zako\u0144czono w dniu:\s*(\d{1,2})\s+(\w+)\s+(\d{4})')
_NUM_RE = re.compile(r'UCHWA\u0141A\s+NR\s+([IVXLCDM]+)\/(\d+)\/(\d{2})', re.I)
_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}
_CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
            "WSTRZYMUJE SIE": "wstrzymal_sie", "BRAK GŁOSU": "brak", "BRAK GLOSU": "brak",
            "NIEOBECNI": "nieobecni"}
_FOOTER_TOKENS = re.compile(
    r'(zakończono|godz|wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|'
    r'głosowanie\s*z\s*dnia|w\s*dniu:|\d{1,2}:\d{2}:\d{2}|\|)', re.I)


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _http(url, cache=None, binary=False):
    ext = ".bin" if binary else ".json"
    if cache:
        import hashlib
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        data = r.read()
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)).write_bytes(data)
    return data if binary else data.decode("utf-8", errors="ignore")


def _clean_name(s):
    s = html.unescape(s or "").strip()
    if not s or not any(c.isalpha() for c in s):
        return None
    if _FOOTER_TOKENS.search(s):
        return None
    return re.sub(r"\s+", " ", s)


def fetch_roster(cache):
    """(name, club_election_committee) z kategorii Skład Rady 2024-2029."""
    d = json.loads(_http(f"{API}/api/menu/{ROSTER_MENU}/articles?limit=60&offset=0", cache))
    out = []
    for a in d.get("articles", []):
        name = club = None
        for cf in a.get("columnFields", []):
            if cf.get("fieldId") == "title":
                name = html.unescape(cf.get("value") or "").strip()
            elif "Stanowisko" not in str(cf.get("fieldId")):
                v = html.unescape(cf.get("value") or "").strip()
                if v and not club and cf.get("fieldId") != "title":
                    club = v
        if name:
            out.append((re.sub(r"\s+", " ", name), club or ""))
    return out


def fetch_vote_attachments(cache):
    """Lista (article_id, attachment_id, uchwala_num) dla wszystkich wyników głosowań IX kad."""
    out = []
    for year, menu in YEAR_MENUS.items():
        offset = 0
        while True:
            d = json.loads(_http(f"{API}/api/menu/{menu}/articles?limit=50&offset={offset}", cache))
            arts = d.get("articles", [])
            if not arts:
                break
            for a in arts:
                title = html.unescape(" ".join(
                    html.unescape(cf.get("value") or "") for cf in a.get("columnFields", [])
                    if cf.get("fieldId") == "title"))
                if not re.search(r'RADY MIEJSK|RADY GMINY|RADA MIEJSKA', title.upper()):
                    continue
                out.append((year, a["id"], title))
            offset += 50
            if offset >= int(d.get("total") or 0):
                break
    return out


def parse_wyniki_pdf(data):
    """PDF 'wyniki_glosowania' -> dict albo None."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return None
    if "Wyniki imienne" not in text:
        return None
    cm = _COUNTS_RE.search(text)
    if not cm:
        return None
    counts = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
              "wstrzymal_sie": int(cm.group(3)), "brak": int(cm.group(4)),
              "nieobecni": int(cm.group(5))}
    gs = text.find("Głosowano w sprawie:")
    topic = ""
    if gs != -1:
        topic = re.sub(r"\s+", " ", text[gs + len("Głosowano w sprawie:"):cm.start()]).strip(" .,:;-")
    dm = _END_DATE_RE.search(text)
    date = None
    if dm:
        mo = _MONTHS.get(dm.group(2).lower())
        if mo:
            date = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
    wi = text.find("Wyniki imienne")
    remainder = text[wi:]
    labels = list(_LABEL_RE.finditer(remainder))
    named = {}
    for i, m in enumerate(labels):
        cat = _CAT_MAP.get(m.group(1).upper().replace("Ę", "Ę"))
        if cat is None:
            cat = _CAT_MAP.get(re.sub(r"[ĘE]", "E", m.group(1).upper()))
        if cat is None:
            continue
        expected = int(m.group(2))
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
        chunk = remainder[start:end]
        for cut in ("Głosowanie zakończono", "Wygenerowano", "|"):
            idx = chunk.find(cut)
            if idx != -1:
                chunk = chunk[:idx]
                break
        chunk = re.sub(r"\s+", " ", chunk)
        tokens = [t for t in (_clean_name(x) for x in chunk.split(",")) if t]
        if len(tokens) != expected:
            return None  # atrybucja nierozpoznawalna — odrzuć cały głos
        named[cat] = tokens
    if not named.get("za") and not named.get("przeciw"):
        return None
    for cat, exp in counts.items():
        if len(named.get(cat, [])) != exp:
            return None
    return {"topic": topic or "(glosowanie)", "date": date, "counts": counts, "named": named}


def make_slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cache = args.cache_dir

    club_by_name = {}
    roster = []
    try:
        roster = fetch_roster(cache)
        print(f"  roster: {len(roster)} radnych (klub wyborczy)")
    except Exception as e:
        print(f"  [warn] roster: {e}")

    arts = fetch_vote_attachments(cache)
    print(f"  uchwał-artefaktów: {len(arts)}")

    # per-article attachments
    votes = []
    seen_att = set()
    for year, aid, title in arts:
        try:
            art = json.loads(_http(f"{API}/api/articles/{aid}", cache))
        except Exception:
            continue
        nm = _NUM_RE.search(title.upper())
        for a in (art.get("attachments") or []):
            name = (a.get("name") or "").lower()
            if "wyniki" not in name or a.get("extension") != "pdf":
                continue
            if a["id"] in seen_att:
                continue
            seen_att.add(a["id"])
            try:
                data = _http(f"{API}/e,pobierz,get.html?id={a['id']}", cache, binary=True)
            except Exception:
                continue
            rec = parse_wyniki_pdf(data)
            if rec is None or not rec.get("date") or rec["date"] < KAD_START:
                continue
            rec["session"] = nm.group(1).upper() if nm else ""
            rec["num"] = f"{nm.group(1)}/{nm.group(2)}" if nm else str(a["id"])
            votes.append(rec)
        time.sleep(0)
    votes.sort(key=lambda v: (v["date"], v["num"]))
    print(f"  głosowań imiennych IX kad: {len(votes)}")

    # ---- build kadencja ----
    sessions_by_date = {}
    all_votes = []
    for i, rec in enumerate(votes, 1):
        d = rec["date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": f"Sesja {rec['session']}" if rec["session"] else d,
                                   "vote_count": 0, "attendees": set()}
        s = sessions_by_date[d]
        s["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak", "nieobecni"):
            s["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(i), "session_date": d, "session_number": rec["session"],
                          "topic": rec["topic"], "named_votes": rec["named"],
                          "counts": {k: len(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
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
    club_lookup = {re.sub(r"[\u2013\u2014]", "-", n): c for n, c in roster}
    councilors_data = {}
    for name in all_names:
        club = club_lookup.get(name) or club_lookup.get(re.sub(r"[\u2013\u2014]", "-", name)) or ""
        councilors_data[name] = {"name": name, "club": club,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c = councilors_data.get(nm)
                if not c:
                    continue
                key = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal",
                       "brak": "votes_brak", "nieobecni": "votes_nieobecny"}[cat]
                c[key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
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
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
                                "activity": None})
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
    club_counts = dict(Counter(c["club"] for c in councilors_list))
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": club_counts,
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out_path.parent / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False), encoding="utf-8")

    profiles = {"scraped_at": datetime.now().isoformat(), "profiles": [], "total": len(councilors_list)}
    for c in councilors_list:
        profiles["profiles"].append({
            "name": c["name"], "slug": make_slug(c["name"]), "club": c["club"],
            "role": "", "photo_url": "", "bio": "", "email": "", "social_links": {},
            "voting": {"za": c["votes_za"], "przeciw": c["votes_przeciw"],
                       "wstrzymal_sie": c["votes_wstrzymal"]},
            "kadencje": {KADENCJA_ID: {
                "club": c["club"], "has_voting_data": True, "has_activity_data": False,
                "role": "", "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
                "zgodnosc_z_klubem": 0.0, "rebellion_count": 0, "former": False, "mid_term": False}},
        })
    Path(args.profiles).write_text(json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
    print(f"  [ok] sesji: {total_sessions}, głosowań: {total_votes}, radnych: {len(councilors_list)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
