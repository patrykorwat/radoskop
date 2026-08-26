#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Świdnica — imienne głosowania Rady Miejskiej.

Źródło: radamiejska.um.swidnica.pl (dedykowany portal Joomla
"Sesje Rady Miejskiej oraz imienne wykazy głosowań radnych" Gminy Miasta Świdnica).
Rada Miejska w Świdnicy (IX kadencja 2024-2029) publikuje per sesję artykuł
"Imienne głosowania Radnych" z listą głosowań ("Głosowanie N [temat]") jako
PDF-y — każdy z wynikiem imiennym (ZA / PRZECIW / WSTRZYMUJĘ SIĘ per radny),
tabela 2-kolumnowa (Lp | Nazwisko i imię | Głos). Parsowane przez pdfplumber
(rozdział kolumn po x<280 / x>=280; nazwiska łamane między wiersze scalane
przez dopasowanie do najbliższego numeru Lp w tej samej kolumnie).

Zakres: 25 sesji (I..XXVII z wyjątkiem VI i XX — sesje bez głosowań imiennych:
powodziowa 04.10.2024 i uroczysta 12.12.2025; XXVIII 2026 dane nieopublikowane),
374 głosowań imiennych, 21 radnych. Radni i kluby kuratorowane z portalu
(/skad-rady-miasta oraz /skad-rady-miasta/kluby-radnych-rm).

Użycie:
    python scrape_swidnica.py --output docs/data.json --profiles docs/profiles.json
                                [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://radamiejska.um.swidnica.pl"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0)"}

# Kanoniczna lista radnych IX kadencji ("Imię Nazwisko") ze strony Skład Rady Miejskiej.
ROSTER = ["Józef Cygan", "Jan Dzięcielski", "Rafał Fasuga", "Edmund Frączak",
          "Joanna Gadzińska", "Krzysztof Grudziński", "Jacek Iwancz",
          "Krzysztof Lewandowski", "Ryszard Makowski", "Danuta Morańska",
          "Tadeusz Niedzielski", "Luiza Nowaczyńska", "Andrzej Ora",
          "Sylwia Osojca-Kozłowska", "Magdalena Rumiancew-Wróblewska",
          "Zofia Skowrońska-Wiśniewska", "Beata Szczepankowska", "Anna Światowa",
          "Violetta Wiercińska", "Michał Zastawny", "Wiesław Żurek"]


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).lower()


ROSTER_TOK = {_norm(r): r for r in ROSTER}
ROSTER_SET = set(ROSTER_TOK)

CLUB_ASSIGN = None  # wypełnione z config.json


STOP = {"głosowanie", "glosowanie", "w", "sprawie", "wniosek", "formalny",
        "przeniesienie", "projektu", "uchwały", "uchwaly", "na", "kolejną",
        "kolejna", "sesję", "sesje", "sesji", "o", "nad", "do", "nr", "oraz",
        "pkt", "punkcie", "wyboru", "powołania", "powolania", "skrutacyjnej",
        "zamknięcia", "zamkniecia", "listy", "kandydatów", "kandydatow",
        "głosowań", "glosowan", "głosowania", "glosowania", "trybie", "art",
        "przewodniczącego", "przewodniczacego", "wiceprzewodniczących",
        "wiceprzewodniczacych", "komisji", "komisja", "uchwał", "uchwal",
        "wniosków", "wnioskow", "numer", "wstrzymuję", "wstrzymuje", "się",
        "sie", "lp.", "nazwisko", "imię", "imie", "głos", "glos"}


# ---------------------------------------------------------------------------
# HTTP / cache
# ---------------------------------------------------------------------------
def _fetch(url: str, session: requests.Session, cache_dir: Path):
    if cache_dir:
        name = urllib.parse.quote(url, safe="") + (".bin" if url.endswith(".pdf") else ".html")
        fp = cache_dir / name
        if fp.exists():
            return fp.read_bytes()
    for attempt in range(3):
        try:
            r = session.get(url, timeout=40)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            return data
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url)


def _pdf_unicode(url: str) -> str:
    """Zakoduj nie-ASCII segmenty ścieżki PDF (nazwy z polskimi znakami/spacjami)."""
    return urllib.parse.quote(url, safe="/:%@?=&")


# ---------------------------------------------------------------------------
# Parsowanie PDF głosowania
# ---------------------------------------------------------------------------
def _parse_vote_pdf(data: bytes):
    """Zwraca dict {lp: {'n': ImięNazwisko, 'v': kategoria}}."""
    import io
    res = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pg in pdf.pages:
                words = pg.extract_words()
                bands = {}
                for w in words:
                    key = round(w["top"] / 3)
                    bands.setdefault(key, []).append(w)
                bands = sorted((k, sorted(v, key=lambda w: w["x0"]))
                               for k, v in bands.items())
                started = False
                anchors = []     # (top, side, lp, vote, namewords)
                names_only = []  # (top, side, namewords)
                for key, ws in bands:
                    for side, half in (("L", [w for w in ws if w["x0"] < 280]),
                                       ("R", [w for w in ws if w["x0"] >= 280])):
                        if not half:
                            continue
                        txt = " ".join(w["text"] for w in half)
                        if "Nazwisko" in txt:      # wiersz nagłówka tabeli
                            started = True
                            continue
                        if "Wydrukowano" in txt:
                            continue
                        if not started:
                            continue
                        lp = None
                        vote = None
                        namewords = []
                        for w in half:
                            t = w["text"]
                            tl = t.lower()
                            if re.fullmatch(r"\d{1,2}\.", t):
                                lp = int(t[:-1])
                            elif t in ("ZA", "PRZECIW", "NIEGŁOSOWAŁ",
                                       "NIEOBECNY", "NIEOBECNA", "BRAK"):
                                vote = t
                            elif t == "WSTRZYMUJĘ":
                                vote = "WSTRZYMUJĘ SIĘ"
                            elif tl == "się" or tl in STOP or re.fullmatch(r"\d+", t):
                                continue
                            else:
                                namewords.append(t)
                        if lp is not None:
                            anchors.append((key, side, lp, vote, namewords))
                        elif namewords:
                            names_only.append((key, side, namewords))
                for key, side, lp, vote, nw in anchors:
                    e = res.setdefault(lp, {"n": "", "v": None})
                    e["v"] = vote or e["v"]
                    for t in nw:
                        e["n"] = (e["n"] + " " + t).strip()
                for key, side, nw in names_only:
                    cands = [(k, lp) for (k, s, lp, v, nn) in anchors if s == side]
                    if not cands:
                        continue
                    _, lp = min(cands, key=lambda cl: abs(cl[0] - key))
                    e = res.setdefault(lp, {"n": "", "v": None})
                    for t in nw:
                        e["n"] = (e["n"] + " " + t).strip()
    except Exception:
        return {}
    return res


_VOTE_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
             "NIEGŁOSOWAŁ": "nieobecni", "NIEOBECNY": "nieobecni",
             "NIEOBECNA": "nieobecni", "BRAK": "brak"}


def _parse_vote_to_names(data: bytes):
    """Zwraca dict {canonical_name: kategoria} dla głosowania."""
    parsed = _parse_vote_pdf(data)
    out = {}
    for lp, e in parsed.items():
        nm = e.get("n", "").strip()
        v = e.get("v")
        if not nm or not v:
            continue
        name = _resolve_name(nm)
        if name:
            out[name] = _VOTE_MAP.get(v, "brak")
    return out


def _resolve_name(raw: str) -> str:
    """'Nazwisko Imię' (z PDF) -> kanoniczne 'Imię Nazwisko'; dopasowanie do rosteru."""
    if raw in ROSTER_SET:
        return raw
    key = _norm(raw)
    if key in ROSTER_TOK:
        return ROSTER_TOK[key]
    parts = raw.split()
    if len(parts) >= 2:
        rev = " ".join(reversed(parts))
        rk = _norm(rev)
        if rk in ROSTER_TOK:
            return ROSTER_TOK[rk]
    return None


# ---------------------------------------------------------------------------
# Kluby
# ---------------------------------------------------------------------------
def _club_of(name: str) -> str:
    if CLUB_ASSIGN:
        return CLUB_ASSIGN.get(name, "NZ")
    return ""


# ---------------------------------------------------------------------------
# Zbieranie danych
# ---------------------------------------------------------------------------
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
          "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
          "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19,
          "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25,
          "XXVI": 26, "XXVII": 27, "XXVIII": 28}


def collect_sessions(session, cache_dir):
    """Ładuje (num, url, glos_art) dla sesji IX kadencji z portalu."""
    from html import unescape
    home = _fetch(BASE + "/", session, cache_dir).decode("utf-8", errors="replace")
    sess = {}
    for m in re.finditer(r'href="(/\d+-sesja-[a-z\-]+)"[^>]*>\s*Sesja\s*([A-Z]+)', home):
        sess.setdefault(m.group(2), BASE + m.group(1))
    items = []
    for num in sorted(sess, key=lambda n: _ROMAN.get(n, 99)):
        url = sess[num]
        d = _fetch(url, session, cache_dir).decode("utf-8", errors="replace")
        m = re.search(r"<main.*?</main>", d, re.S)
        seg = m.group(0) if m else d
        gl = re.findall(r'href="([^"]*imienne-glosowania-radnych[^"]*)"', seg)
        glos_art = None
        if gl:
            a = gl[0]
            glos_art = a if a.startswith("http") else (BASE + a if a.startswith("/") else a)
        items.append({"num": num, "url": url, "glos_art": glos_art})
        time.sleep(0.2)
    return items


def collect_records(sessions, session, cache_dir):
    """Zwraca listę rekordów głosowań."""
    records = []
    for s in sessions:
        if not s.get("glos_art"):
            print(f"    sesja {s['num']}: brak artykułu 'Imienne głosowania Radnych' (brak głosowań)")
            continue
        d = _fetch(s["glos_art"], session, cache_dir).decode("utf-8", errors="replace")
        m = re.search(r"<main.*?</main>", d, re.S)
        seg = m.group(0) if m else d
        pdfs = re.findall(r'href="([^"]*attachments/article/[^"]+\.pdf)"', seg)
        # dedupe zachowując kolejność (href pojawia się 2x na stronie)
        seen = set()
        pdfs = [p for p in pdfs if not (p in seen or seen.add(p))]
        # uszereguj głosowania po kolejności na stronie
        vote_items = []
        for pdf in pdfs:
            vote_items.append({"topic": None, "pdf": pdf})
        if not vote_items:
            continue
        n_votes = 0
        n_fail = 0
        for vi in vote_items:
            pdf_url = vi["pdf"]
            if pdf_url.startswith("/"):
                pdf_url = BASE + pdf_url
            try:
                data = _fetch(_pdf_unicode(pdf_url), session, cache_dir)
            except Exception as e:
                print(f"      sesja {s['num']}: błąd pobrania PDF {pdf_url}: {e}")
                n_fail += 1
                continue
            named_raw = _parse_vote_to_names(data)
            if not named_raw:
                n_fail += 1
                continue
            named = defaultdict(list)
            for _nm, _cat in named_raw.items():
                named[_cat].append(_nm)
            named = dict(named)
            n_votes += 1
            records.append({"session_num": s["num"], "named": named,
                            "topic": vi.get("topic") or "", "pdf": pdf_url})
            time.sleep(0.15)
        print(f"    sesja {s['num']}: {n_votes} głosowań (fail {n_fail})")
    return records


# ---------------------------------------------------------------------------
# Build output (kadencja + data.json + profiles.json)  — wzorzec jastrzebie
# ---------------------------------------------------------------------------
def _session_date(records):
    """Najwcześniejsza data 'Data głosowania' z PDF — użyta jako data sesji."""
    # data głosowania jest w nagłówku PDF; bierzemy ją przy parsowaniu -> do records
    return None


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        num = rec.get("session_num", "")
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": num, "vote_count": 0,
                                   "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": num,
            "topic": rec.get("topic") or "",
            "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"],
                              "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]),
                              "speakers": []})

    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)

    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": _club_of(name),
                                 "district": None, "votes_za": 0,
                                 "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0,
                                 "votes_with_club": 0, "votes_against_club": 0,
                                 "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                if name not in councilors_data:
                    continue
                c = councilors_data[name]
                if cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    c["votes_wstrzymal"] += 1
                elif cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for n in names:
                    councillor_sess[n].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) \
            if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"],
                                "district": None,
                                "frekwencja": round(frekwencja, 1),
                                "aktywnosc": round(aktywnosc, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"],
                                "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"],
                                "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"],
                                "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vv in common if vectors[a][vv] == vectors[b][vv])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1),
                      "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(),
            "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = ("za" if cat == "za" else "przeciw" if cat == "przeciw"
                       else "wstrzymal_sie" if cat == "wstrzymal_sie"
                       else "nieobecny" if cat == "nieobecni" else "brak")
                cv[name][key] += 1
                cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({"name": name, "slug": _slug(name),
                         "kadencje": {KADENCJA_ID: {
                             "club": _club_of(name), "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(frekw, 1), "aktywnosc": 0.0,
                             "zgodnosc_z_klubem": 0.0, "votes_za": vd["za"],
                             "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": vd["brak"], "votes_nieobecny": vd["nieobecny"],
                             "votes_total": total, "rebellion_count": 0,
                             "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    return {"profiles": profiles}


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    global CLUB_ASSIGN
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if args.config and Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        CLUB_ASSIGN = cfg.get("club_assignments") or {}

    session = requests.Session()
    session.headers.update(UA)

    print("=== Scraper Rada Miejska Świdnica (radamiejska.um.swidnica.pl) ===")
    sessions = collect_sessions(session, cache_dir)
    print(f"  Sesje IX kadencji: {len(sessions)}")

    records = collect_records(sessions, session, cache_dir)

    # data sesji z daty pierwszego głosowania (datePublished w nagłówku PDF)
    import io
    print("  Przypisywanie dat sesji z nagłówków PDF...")
    dates_by_session = {}
    for rec in records:
        if rec["session_num"] in dates_by_session:
            continue
        data = _fetch(_pdf_unicode(rec["pdf"]), session, cache_dir)
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                t = pdf.pages[0].extract_text() or ""
            m = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
            if m:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                dates_by_session[rec["session_num"]] = f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
        time.sleep(0.1)
    for rec in records:
        rec["session_date"] = dates_by_session.get(rec["session_num"], "")

    records = [r for r in records if r.get("session_date")]
    print(f"  Rekordy głosowań z datą: {len(records)}")
    print(f"  Sesje: {len(set(r['session_date'] for r in records))}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    kad = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Głosowań: {kad['total_votes']}, sesji: {kad['total_sessions']}, "
          f"radnych: {kad['total_councilors']}")


if __name__ == "__main__":
    main()
