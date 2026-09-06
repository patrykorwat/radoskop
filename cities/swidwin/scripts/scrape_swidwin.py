#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Świdwin — imienne głosowania Rady Miasta Świdwin (IX kadencja).

Źródło: BIP swidwin.biuletyn.net (platforma biuletyn.net), kategorie
"Uchwały Rady Miasta" per rok: cid=1492 (2024 IX kad.), cid=1507 (2025), cid=1522 (2026).
Każdej uchwale towarzyszy załącznik PDF "wyniki_glosowania_N.pdf" generowany z systemu
DSSS Vote App (skan + warstwa tekstowa): nagłówek 'Uchwała numer X/NN/RR „…” została
podjęta … proporcją głosów: jestem za N, jestem przeciw M, wstrzymuję się K',
'Data i godzina głosowania: RRRR-MM-DD', dwukolumnowa tabela: LEWA kolumna = ZA,
PRAWA (x>340) = PRZECIW; pod tabelą po LEWEJ 'Wstrzymuję się' lista WSTRZYMUJĄCYCH,
po PRAWEJ 'Obecni radni, którzy nie wzięli udziału' = nieobecni przy głosowaniu.

Parser pdfplumber word-coords; naprawy OCR (q→ą w nazwiskach, sklejone ImięNazwisko,
'1 .B arbara'); wiersze bez numeru = kontynuacja zawiniętego nazwiska; normalizacja
nazwisk do kanonu składu. Walidacja per głos: listy nazwisk == agregaty nagłówka,
głos niezwalidowany odrzucany (NIE fabrykujemy).

Użycie: python scrape_swidwin.py --city-dir <cities/swidwin> [--cache-dir dir]
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

BASE = "https://swidwin.biuletyn.net"
CATS = ["1492", "1507", "1522"]  # Uchwały Rady Miasta: 2024 (od IX kad.), 2025, 2026
KAD_START = "2024-05-06"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PRZECIW_X = 340.0

UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0; info@radoskop.eu)"}
REQ_DELAY = 0.5
_LAST = 0.0

_ROM = {r: i for i, r in enumerate(
    ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","XIII","XIV","XV","XVI",
     "XVII","XVIII","XIX","XX","XXI","XXII","XXIII","XXIV","XXV","XXVI","XXVII","XXVIII",
     "XXIX","XXX","XXXI","XXXII","XXXIII","XXXIV","XXXV","XXXVI","XXXVII","XXXVIII"], 1)}

# Kanon nazwisk (skład Rady Miasta Świdwin IX kadencji, 15 radnych). Klucz =
# frozenset znormalizowanych tokenów nazwy (bez diakrytyków/ł->l, bez dywizów).
# Źródło: warstwy tekstowe raportów DSSS Vote (stabilny skład; Komorowska ->
# Komorowska-Kowalska w trakcie kadencji — ta sama osoba, Michalina).
_ROSTER_RAW = [
    "Barbara Bujak", "Mirosław Dereń", "Krzysztof Kajder", "Łukasz Łemańczyk",
    "Jerzy Konat", "Justyna Kondracka", "Monika Makowska", "Izabela Markiewicz",
    "Marcin Popow", "Regina Barszcz", "Michalina Komorowska-Kowalska",
    "Wojciech Piątek", "Anna Parchoć", "Klaudyna Zajchowska", "Jolanta Kaliszewska",
]

def norm_key(s):
    """Klucz porównawczy nazwiska: lower, ł->l, bez diakrytyków/spacji/dywizów."""
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _tokset(s):
    return frozenset(norm_key(w) for w in re.split(r"[\s\-]+", s) if w.strip())

ROSTER_BY_SET = {_tokset(n): n for n in _ROSTER_RAW}
ROSTER_BY_SET[_tokset("Michalina Komorowska")] = "Michalina Komorowska-Kowalska"


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None):
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    data = r.content
    if cf is not None:
        cf.write_bytes(data)
    return data


def make_slug(name):
    repl = {"ą":"a","ć":"c","ę":"e","ł":"l","ń":"n","ó":"o","ś":"s","ź":"z","ż":"z"}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def canon_name(s):
    """Znormalizuj OCR-ową nazwę do kanonu składu (lub zwróć oczyszczoną)."""
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    s = re.sub(r"(?<=[a-ząćęłńóśźż])(?=[A-ZŁŚŹŻ])", " ", s)  # sklejone ImięNazwisko
    s = re.sub(r"\b([A-ZŁŚŹŻ])\s+(?=[a-ząćęłńóśźż])", r"\1", s)  # 'B arbara'->'Barbara'
    s = re.sub(r"\bMi\s*c?\s*h?\s*a?lina\b", "Michalina", s)  # 'Mich alina'/'Mi chaljna'
    s = re.sub(r"(?<=\w)q(?=\w)", "ą", s)  # OCR q jako ą (np. 'Piqtek' -> 'Piątek')
    disp, exact = _match_roster(s)
    if exact:
        return disp, True
    return disp, False


def _match_roster(s):
    """Dopasuj do kanonu; zwróć (display, exact). Kolejność tokenów bez znaczenia."""
    toks = [t for t in re.split(r"[\s\-]+", s) if t.strip()]
    # zlewaj kontynuacje małą literą: 'Komoro wska' -> 'Komorowska'
    merged = []
    for t in toks:
        if merged and re.match(r"^[a-ząćęłńóśźż]", t):
            merged[-1] += t
        else:
            merged.append(t)
    toks = merged
    key = frozenset(norm_key(t) for t in toks if norm_key(t))
    if not key:
        return s, False
    if key in ROSTER_BY_SET:
        return ROSTER_BY_SET[key], True
    # podzbiór tokenów (np. samo nazwisko) — dokładnie jeden kandydat
    cands = {d for k, d in ROSTER_BY_SET.items() if key <= k}
    if len(cands) == 1:
        return next(iter(cands)), True
    # fuzzy: każdy token pasuje (ratio>=0.75) do tokenu TYCH SAMYCH członków
    import difflib
    member_cands = None
    for t in key:
        hits = set()
        for k, d in ROSTER_BY_SET.items():
            if any(t == kt or difflib.SequenceMatcher(None, t, kt).ratio() >= 0.78 for kt in k):
                hits.add(d)
        member_cands = hits if member_cands is None else (member_cands & hits)
        if not member_cands:
            break
    if member_cands and len(member_cands) == 1 and len(key) >= 2:
        return next(iter(member_cands)), True
    return s, False


_NUMTOK = re.compile(r"^([0-9Ss5]\.?)|(\.)$")


def _strip_num(toks):
    """Usuń z początku wiersza token numeru wiersza (1., 10., S., '.' osobno)."""
    i = 0
    while i < len(toks) and re.fullmatch(r"[0-9Ss5]{1,3}\.|\.", toks[i]):
        i += 1
    if i == 0 and len(toks) >= 2 and re.fullmatch(r"[0-9Ss5]{1,3}", toks[0]) and toks[1] == ".":
        i = 2
    return toks[i:]


def _has_num(toks):
    if not toks:
        return False
    return bool(re.fullmatch(r"[0-9Ss5]{1,3}\.|\.", toks[0])) or \
           (len(toks) >= 2 and re.fullmatch(r"[0-9Ss5]{1,3}", toks[0]) and toks[1] == ".")


HDR_WORDS = ("Wstrzymuj", "Obecni", "radni", "udziału", "głosowaniu", "którzy", "wzięli", "BRAK", "Operatorem", "Wygenerowano")


def parse_vote_pdf(data, fname=""):
    """Jedno głosowanie per PDF -> dict lub None."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        t = re.sub(r"\s+", " ", page.extract_text() or "")
        words = page.extract_words()
    m = re.search(
        r"proporcj.*?jestem za\s*([\dOo]+)[,.]?\s*jestem przeciw\s*([\dOo]+)[,.]?\s*"
        r"wstrzymuj[eę]?\s*si[eę]\s*([\dOo]+)", t)
    if not m:
        return None
    def _d(s):
        return int(s.replace("O", "0").replace("o", "0"))
    za_n, pp_n, ws_n = _d(m.group(1)), _d(m.group(2)), _d(m.group(3))
    dm = re.search(r"godzina\s*g.{0,2}?osowania:\s*(\d{4}-\d{2}-\d{2})", t)
    if not dm:
        return None
    date = dm.group(1)
    um = re.search(r"Uchwa[ał]a numer\s+([0-9OoIiVvXxLl/\-\.]{2,14})\b", t)
    uchnum = um.group(1).rstrip(".") if um else ""
    # numer sesji: z nazwy pliku (UCHWALA_I_1_24) albo z uchnum (XXIX/212/26)
    sess = None
    fm = re.search(r"UCHWALA_([IVXLCDM]+)_\d+_\d{2}", fname.upper())
    if fm:
        sess = _ROM.get(fm.group(1))
    if sess is None and uchnum and "/" in uchnum and re.search(r"[A-Za-z]", uchnum.split("/")[0]):
        p0 = re.sub(r"[0Oo]", "", uchnum.split("/")[0]).upper()
        sess = _ROM.get(p0)
    # temat
    topic = ""
    tm = re.search(r"(?:[UHK]w|[\"„])?\s*(w sprawie.*?)\s*[\"”„]\s*zosta", t)
    if tm:
        topic = tm.group(1).strip()
    if not topic:
        topic = f"Uchwała {uchnum}" if uchnum else "(głosowanie)"

    # wiersze tabeli (greedy clustering z tolerancją ~4pt)
    ws = sorted((w for w in words if w["top"] >= 240), key=lambda w: (w["top"], w["x0"]))
    lines = []
    cur = []
    for w in ws:
        if cur and w["top"] - min(x["top"] for x in cur) > 4.5:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    lines = [(round(min(x["top"] for x in lw), 1), sorted(lw, key=lambda w: w["x0"])) for lw in lines]
    y_head = y_wsz = y_abs = y_foot = None
    ys_jestem = [y for y, lw in lines if any(w["text"] == "Jestem" for w in lw) and y < 400]
    if ys_jestem:
        y_head = min(ys_jestem) - 6.0  # nagłówek kolumn; tabela zaczyna się niżej
    for y, lw in lines:
        if y_head is None:
            continue
        if y_wsz is None and any(w["x0"] < PRZECIW_X and w["text"].startswith("Wstrzymuj") for w in lw):
            y_wsz = y
        if y_abs is None and any(w["x0"] >= PRZECIW_X - 60 and w["text"] in ("Obecni", "radni,") for w in lw):
            y_abs = y
        if any(w["text"] in ("Operatorem", "Wygenerowano") for w in lw):
            y_foot = y
            break
    if y_head is None:
        return None

    def line_cols(lw):
        left = [w["text"] for w in lw if w["x0"] < PRZECIW_X]
        right = [w["text"] for w in lw if w["x0"] >= PRZECIW_X]
        return left, right

    # --- strumień tokenów kolumny z pozycjami tokenów kanonu ---
    def member_at(toks, i):
        """Dopasuj w toks[i:] najdłuższe pasmo tokenów = członek składu (tolerancja OCR).
        Zwraca (display, consumed) albo (None, 0)."""
        for ln in (4, 3, 2, 1):
            if i + ln > len(toks):
                continue
            sub = [t for t in toks[i:i + ln] if not re.fullmatch(r"[0-9Ss5]{1,3}\.|\.", t)]
            if not sub:
                continue
            disp, exact = canon_name(" ".join(sub))
            if exact:
                return disp, ln
        return None, 0

    def scan_column(word_lists):
        """Z list słów (w kolejności wierszy) wyciągnij kolejnych członków składu."""
        found = []
        for wl in word_lists:
            toks = list(wl)
            i = 0
            while i < len(toks):
                nm, consumed = member_at(toks, i)
                if nm:
                    if not found or found[-1] != nm:
                        found.append(nm)
                    i += consumed
                else:
                    i += 1
        return found

    kept = [(y, lw) for y, lw in lines if y > y_head + 2 and not (y_foot and y >= y_foot - 2)]
    za_rows, wsz_rows, pp_rows, abs_rows = [], [], [], []
    for y, lw in kept:
        left = [w["text"] for w in lw if w["x0"] < PRZECIW_X]
        right = [w["text"] for w in lw if w["x0"] >= PRZECIW_X]
        if y_wsz is not None and y >= y_wsz - 1:
            wsz_rows.append([w for w in left if not w.startswith("Wstrzymuj")])
        else:
            za_rows.append(left)
        if y_abs is not None and y >= y_abs - 1:
            abs_rows.append([w for w in right if w not in ("Obecni", "radni,", "którzy", "nie", "wzięli", "udziału", "w", "głosowaniu")])
        else:
            pp_rows.append(right)

    za_found = scan_column(za_rows)
    ws_found = scan_column(wsz_rows)
    pp_found = scan_column(pp_rows)
    ab_found = scan_column(abs_rows)

    ok = (len(za_found) == za_n and len(pp_found) == pp_n and len(ws_found) == ws_n)
    return {"uchnum": uchnum, "topic": topic, "date": date, "session": sess,
            "za": za_found, "przeciw": pp_found, "wstrz": ws_found, "abs": ab_found,
            "counts": (za_n, pp_n, ws_n), "validated": ok}


def list_vote_files():
    """(cid, path, fname) wszystkich raportów wyników głosowań (dedup po nazwie pliku)."""
    out = []
    seen = set()
    for cid in CATS:
        html = _get(f"{BASE}/?bip=1&cid={cid}&bsc=N").decode("utf-8", "replace")
        for p in re.findall(r'(fls/bip_pliki/[^"]+/(?i:wyniki_glosowani\w*)\.pdf)', html):
            fn = p.split("/")[-1]
            if fn in seen:
                continue
            seen.add(fn)
            out.append((cid, p, fn))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    files = list_vote_files()
    print(f"[swidwin] {len(files)} plików wyników głosowań")
    votes = []
    unmatched = Counter()
    for cid, p, fname in files:
        try:
            data = _get(BASE + "/" + p, cache)
        except Exception as e:
            print(f"  [ERR dl {fname}] {type(e).__name__}")
            continue
        try:
            v = parse_vote_pdf(data, fname)
        except Exception as e:
            print(f"  [ERR parse {fname}] {type(e).__name__}: {e}")
            continue
        if v is None:
            print(f"  [skip {fname}] brak nagłówka DSSS")
            continue
        if not v["validated"]:
            print(f"  [skip-unverified {fname}] counts={v['counts']} parsed=({len(v['za'])},{len(v['przeciw'])},{len(v['wstrz'])})")
            continue
        if v["date"] < KAD_START:
            continue
        votes.append(v)
    print(f"[swidwin] zwalidowane: {len(votes)}")
    votes.sort(key=lambda v: (v["date"], v["uchnum"]))

    # dopisanie brakujących numerów sesji: głos z nieczytelnym uchnum w danym dniu
    # dostaje numer sesji ustalony dla większości głosów tego dnia
    day_sess = defaultdict(Counter)
    for v in votes:
        if v["session"]:
            day_sess[v["date"]][v["session"]] += 1
    for v in votes:
        if not v["session"] and day_sess.get(v["date"]):
            v["session"] = day_sess[v["date"]].most_common(1)[0][0]

    # kanonizacja nazwisk
    def canon_list(names):
        out = []
        for nm in names:
            disp, okf = canon_name(nm)
            if not okf:
                unmatched[nm] += 1
            out.append(disp)
        return out

    sessions_by_date = {}
    all_votes = []
    vid = 0
    for v in votes:
        d = v["date"]
        num = v["session"] or ""
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": num, "vote_count": 0, "attendees": set(), "speakers": []}
        s = sessions_by_date[d]
        if num and not s["number"]:
            s["number"] = num
        s["vote_count"] += 1
        named = {"za": canon_list(v["za"]), "przeciw": canon_list(v["przeciw"]),
                 "wstrzymal_sie": canon_list(v["wstrz"]), "nieobecni": canon_list(v["abs"])}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            s["attendees"].update(named[cat])
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": num,
                          "topic": v["topic"], "resolution": v["uchnum"],
                          "named_votes": named,
                          "counts": {"za": v["counts"][0], "przeciw": v["counts"][1], "wstrzymal_sie": v["counts"][2]}})
    if unmatched:
        print("[swidwin] NAZWISKA BEZ KANONU:", dict(unmatched))
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
    cdata = {nm: {"name": nm, "club": club_assign.get(nm, "NZ"), "district": None,
                  "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                  "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []} for nm in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c = cdata.get(nm)
                if not c:
                    continue
                if cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                elif cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                else:
                    c["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    csess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                csess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(cdata.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(csess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors), 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    data_json = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
                 "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data_json, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = []
    n_sess = total_sessions or 1
    for c in councilors_list:
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        profiles.append({"name": c["name"], "slug": make_slug(c["name"]),
            "kadencje": {KADENCJA_ID: {"club": c["club"], "has_voting_data": True,
                "has_activity_data": False, "frekwencja": c["frekwencja"], "aktywnosc": round(present / n_sess * 100, 1),
                "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": 0, "votes_nieobecny": c["votes_nieobecny"],
                "votes_total": present, "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    (docs / "profiles.json").write_text(json.dumps({"profiles": profiles, "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[swidwin] DONE votes={total_votes} sessions={total_sessions} councilors={len(councilors_list)}")


if __name__ == "__main__":
    main()
