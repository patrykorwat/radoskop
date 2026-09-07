#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Drawsko Pomorskie — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP umdrawsko.bip.gov.pl (SSDIP), kategoria
/wladze-gminy/ix-kadencji-2024-2029/sesje-glosowania/glosowania-na-sesji.html —
30 załączników PDF "Raport Głosowania" (I.2024 z 06.05.2024 … XXX.2026 z 12.08.2026),
ścieżki /fobjects/download/<id>/<x>.pdf. Każdy PDF = jedna sesja, JEDEN GŁOS NA STRONĘ.

Format strony (pdfplumber, układ dwukolumnowy — prawa kolumna x0>=415 to tabela imienna):
    Lewa: Numer: <SESJIA>/<n>/<rok> | Data głosowania: D miesiąc RRRR, HH:MM:SS - ... |
          Głosowanie: JAWNE Większość: ... | Tekst głosowania: <temat> |
          WYNIKI GŁOSOWANIA  ZA n  PRZECIW n  WSTRZYMAŁO SIĘ n |
          Uprawnionych do głosowania: n | Głosowało: n | Uchwała została uchwalona/...
    Prawa (x0>=415): nagłówek 'Imię i nazwisko | Głosował(a) | Jak głosował(a)' +
          wiersze '<Imię> <Nazwisko> [TAK|NIE] <ZA|PRZECIW|WSTRZYMAŁ SIĘ|NIEOBECNY>'
          (x rzędu: nazwisko 415-595, Głosował(a) ~600-650, głos >=660; NIEOBECNY ~601).

Parser per strona na współrzędnych (extract_words + klasteryzacja wierszy po top±4):
  - głos = token(e) z x0>=660 na wierszu (ZA / PRZECIW / WSTRZYMAŁ(+SIĘ) / WSTRZYMAŁA / NIEOBECNY/NA),
    NIEOBECNY łapany też przy x0>=595 (bo zaczyna się w kolumnie 'Głosował(a)');
  - nazwisko = tokeny z 415<=x0<595 na tym samym wierszu;
  - kolejność IMIĘ/Nazwisko normalizowanawiększością głosów wystąpień (źródło bywa
    'Imię Nazwisko' albo 'Nazwisko Imię' — np. 'Skrzypczak Michał');
  - walidacja per głos: zliczone ZA/PRZECIW/WSTRZYMAŁ == agregaty lewej kolumny;
    rozbieżność -> głos odrzucony (brak atrybucji, nigdy częściowa).

Roster = unikalne nazwiska z głosów (dynamiczny w razie zmian kadencji).

Użycie:
    python scrape_drawsko_pomorskie.py --output docs/data.json --profiles docs/profiles.json \
        [--cache-dir DIR]
"""
import argparse
import hashlib
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

BIP = "https://umdrawsko.bip.gov.pl"
CAT_URL = f"{BIP}/wladze-gminy/ix-kadencji-2024-2029/sesje-glosowania/glosowania-na-sesji.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Radoskop/1.0 (info@radoskop.eu)",
      "Accept-Language": "pl"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
REQ_DELAY = 0.5
_LAST = 0.0

NAME_X0 = 415
NAME_X1 = 595
VOTE_X = 660
NIEOB_X = 595

_MONTHS = {"stycze\u0144": 1, "luty": 2, "marzec": 3, "kwiecie\u0144": 4, "maj": 5, "czerwiec": 6,
           "lipiec": 7, "sierpie\u0144": 8, "wrzesie\u0144": 9, "pa\u017adziernik": 10,
           "listopad": 11, "grudzie\u0144": 12,
           "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "wrze\u015bnia": 9, "pa\u017adziernika": 10,
           "listopada": 11, "grudnia": 12}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _http(url, cache=None, binary=False):
    ext = ".bin" if binary else ".html"
    if cache:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        data = r.read()
    if cache:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)).write_bytes(data)
    return data if binary else data.decode("utf-8", errors="replace")


def discover_sessions(cache):
    """[(filename, download_url)] dla sesji IX kad z listy załączników kategorii."""
    html_txt = _http(CAT_URL, cache)
    links = re.findall(r'href="(/fobjects/download/\d+/[^"]+)"[^>]*>\s*([^<]*\.pdf)', html_txt, re.I)
    out = []
    seen = set()
    for href, name in links:
        url = BIP + href
        if url in seen:
            continue
        seen.add(url)
        out.append((name.strip(), url))
    return out


def _cluster_lines(words, tol=4):
    lines = []
    for w in sorted(words, key=lambda w: w["top"]):
        if lines and abs(w["top"] - lines[-1][0]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    return [(t, sorted(ws, key=lambda w: w["x0"])) for t, ws in lines]


def _vote_of(row_words):
    """Zwróć (kategoria_glosu) dla tokeny głosu wiersza lub None."""
    tail = [w["text"].upper() for w in row_words]
    joined = " ".join(tail)
    if "NIEOBECNY" in joined or "NIEOBECNA" in joined:
        return "nieobecni"
    if "WSTRZYMA" in joined:
        return "wstrzymal_sie"
    if "PRZECIW" in joined:
        return "przeciw"
    if re.search(r"\bZA\b", joined):
        return "za"
    return None


def parse_session_pdf(data):
    """PDF sesji -> lista rekordów głosów (per strona) + nazwa sesji z nagłówka."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = list(pdf.pages)
    except Exception:
        return []
    records = []
    for pg in pages:
        text = pg.extract_text() or ""
        if "Raport G\u0142osowania" not in text and "Raport Glosowania" not in text:
            continue
        nm = re.search(r"Numer:\s*([IVXLCDM]+)\s*/\s*(\d+)", text, re.I)
        if not nm:
            continue
        session = nm.group(1).upper()
        vnum = f"{nm.group(1)}/{nm.group(2)}"
        dm = re.search(r"Data g\u0142osowania:\s*(\d{1,2})\s+(\w+)\s+(\d{4})", text)
        date = None
        if dm:
            mo = _MONTHS.get(dm.group(2).lower())
            if mo:
                date = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
        if not date:
            continue
        # temat: lewa kolumna, od 'Tekst' do 'WYNIKI' (dwukropek bywa pomijany)
        tm = re.search(r"Tekst\s+(?:g\u0142osowania:?)?\s*(.{0,300}?)(?:WYNIKI|Wyniki)", text, re.S)
        topic = re.sub(r"\s+", " ", tm.group(1)).strip(" .,:;-") if tm else ""
        topic = topic[:300] or "(glosowanie)"
        # agregaty lewej kolumny
        agg = {}
        for cat, pat in (("za", r"\bZA\s+(\d+)"),
                        ("przeciw", r"PRZECIW\s+(\d+)"),
                        ("wstrzymal_sie", r"WSTRZYMA\u0141O SI[ĘE]\s+(\d+)|WSTRZYMY\u0141O SI[ĘE] SI[ĘE]\s+(\d+)")):
            m = re.search(pat, text)
            if m:
                agg[cat] = int(next(g for g in m.groups() if g))
        words = pg.extract_words()
        named = defaultdict(list)
        row_ok = True
        for top, ws in _cluster_lines(words):
            name_ws = [w for w in ws if NAME_X0 <= w["x0"] < NAME_X1]
            vote_ws = [w for w in ws if w["x0"] >= NIEOB_X and
                       (w["x0"] >= VOTE_X or w["text"].upper().startswith("NIEOBECN"))]
            if not name_ws or not vote_ws:
                continue
            cat = _vote_of(vote_ws)
            if cat is None:
                continue
            # wiersz musi miec wyraźną kolumnę 'Głosował(a)': TAK/NIE (chyba NIEOBECNY)
            tail = " ".join(w["text"].upper() for w in ws if w["x0"] >= NAME_X1)
            if cat != "nieobecni" and not re.search(r"\b(TAK|NIE)\b", tail):
                row_ok = False
            name = re.sub(r"\s+", " ", " ".join(w["text"] for w in name_ws)).strip()
            name = name.lstrip("-–•* ").strip()
            if not name or name.lower().startswith("imi"):
                continue
            named[cat].append(name)
        counts = {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie", "nieobecni")}
        table_rows = sum(counts.values())
        # Walidacja vs agregaty. Zródło bywa niespójne (uprawnionych < wierszy
        # tabeli — błąd eSesja, np. sesja XXIX.2026): wtedy akceptujemy tabelę
        # imienną jeśli KAŻDY wiersz ma wyraźne TAK/NIE (atrybucja pełna).
        uprawn = re.search(r'Uprawnionych do g\u0142osowania:\s*(\d+)', text)
        agg_consistent = uprawn is None or int(uprawn.group(1)) == table_rows
        ok = row_ok
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            if cat in agg and agg[cat] != counts.get(cat, 0):
                ok = ok and not agg_consistent and row_ok
        if not ok or (counts["za"] + counts["przeciw"] + counts["wstrzymal_sie"]) == 0:
            continue
        records.append({"session": session, "num": vnum, "date": date, "topic": topic,
                        "named": {k: v for k, v in named.items() if v}, "counts": counts})
    return records


def normalize_order(names):
    """Kanonizacja 'Nazwisko Imię' -> 'Imię Nazwisko' przez lib_names_pl.fix_name_order."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from lib_names_pl import fix_name_order
    return {u: fix_name_order(u) for u in sorted(set(names))}


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

    sessions = discover_sessions(cache)
    print(f"  sesji-plików PDF: {len(sessions)}")
    all_recs = []
    for name, url in sessions:
        try:
            data = _http(url, cache, binary=True)
        except Exception as e:
            print(f"  [warn] {name}: {e}")
            continue
        recs = parse_session_pdf(data)
        ok = sum(1 for r in recs)
        print(f"  {name}: {ok} głosów")
        all_recs.extend(recs)
    all_recs.sort(key=lambda r: (r["date"], r["num"]))

    raw_names = []
    for r in all_recs:
        for names in r["named"].values():
            raw_names.extend(names)
    canon = normalize_order(raw_names)

    votes = []
    sessions_by_date = {}
    for i, rec in enumerate(all_recs, 1):
        d = rec["date"]
        named = {k: [canon.get(n, n) for n in v] for k, v in rec["named"].items()}
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": f"Sesja {rec['session']}",
                                   "vote_count": 0, "attendees": set()}
        s = sessions_by_date[d]
        s["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni"):
            s["attendees"].update(named.get(cat, []))
        votes.append({"id": str(i), "session_date": d, "session_number": rec["session"],
                      "topic": rec["topic"], "named_votes": named,
                      "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})

    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {n: {"name": n, "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                           "votes_brak": 0, "votes_nieobecny": 0} for n in all_names}
    for v in votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c = councilors_data.get(nm)
                if not c:
                    continue
                key = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal",
                       "brak": "votes_brak", "nieobecni": "votes_nieobecny"}[cat]
                c[key] += 1
    total_votes = len(votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in votes:
        for names in v["named_votes"].values():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": "", "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
                                "activity": None})
    vectors = defaultdict(dict)
    for v in votes:
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
           "councilors": councilors_list, "votes": votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"generated": datetime.now().isoformat(),
                                    "default_kadencja": KADENCJA_ID, "kadencje": [kad]},
                                   ensure_ascii=False), encoding="utf-8")
    (out_path.parent / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False), encoding="utf-8")
    profiles = {"scraped_at": datetime.now().isoformat(), "profiles": [], "total": len(councilors_list)}
    for c in councilors_list:
        profiles["profiles"].append({
            "name": c["name"], "slug": make_slug(c["name"]), "club": "",
            "role": "", "photo_url": "", "bio": "", "email": "", "social_links": {},
            "voting": {"za": c["votes_za"], "przeciw": c["votes_przeciw"], "wstrzymal_sie": c["votes_wstrzymal"]},
            "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True, "has_activity_data": False,
                                       "role": "", "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
                                       "zgodnosc_z_klubem": 0.0, "rebellion_count": 0,
                                       "former": False, "mid_term": False}},
        })
    Path(args.profiles).write_text(json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
    print(f"  [ok] sesji: {total_sessions}, głosowań: {total_votes}, radnych: {len(councilors_list)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
