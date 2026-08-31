#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Połczyn-Zdrój — imienne głosowania Rady Miejskiej (DSSS posiedzenia.pl,
wykazy tekstowe PDF na BIP umig.polczynzdroj.ibip.pl, IX kadencja 2024-2029).

Źródło:
  http://umig.polczynzdroj.ibip.pl/public/?id=61710  — "Sesje Rady Miejskiej i protokoły z sesji"
    → "Kadencja 2024-2029" (id=253732) → lata (2024/2025/2026) → podkategorie per-sesja
    → plik "Wykaz głosowań" (/public/getFile?id=N).
  PDF = system DSSS "posiedzenia.pl", format TEKSTOWY: per-głosowanie blok
    "głosowanie / druk nr N – <temat> / jednostka / wynik / data / typ /
    Podsumowanie (ZA/PRZECIW/WSTRZYMAŁO SIĘ + ilości) / Wyniki imienne (lp/
    nazwisko/imię/głos)" — pełna atrybucja per radny w warstwie tekstowej.

Walidacja: KAŻDE głosowanie reconcilowane vs agregat (ZA+PRZECIW+WSTRZ == suma
list imiennych); nie-reconcilowane pomijane (nie fabrykujemy).

Roster (walidacja): 15 radnych IX kad. — BIP "Skład Rady Miejskiej" (id=11115).

Użycie:  python scrape_polczyn_zdroj.py --city-dir cities/polczyn-zdroj
"""
import argparse
import json
import re
import ssl
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf

BASE = "http://umig.polczynzdroj.ibip.pl"
ENTRY_URL = f"{BASE}/public/?id=61710"          # Sesje Rady Miejskiej i protokoły z sesji
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.45

_ROSTER = ["Robert Dośpiał", "Józef Gąska", "Olga Grzelak", "Jerzy Jacewicz",
           "Lucyna Korszyłowska", "Cezary Makowski", "Patrycja Nowak",
           "Marzena Ostrowska-Olejnicka", "Mariusz Rutkowski", "Adam Słabkowski",
           "Krzysztof Stefanio", "Paweł Świebodzki", "Aneta Wasilewska",
           "Alina Wiśniewska", "Izabela Zawojska"]

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
          "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
          "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
          "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
          "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32,
          "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36, "XXXVII": 37,
          "XXXVIII": 38, "XXXIX": 39, "XL": 40, "XLI": 41, "XLII": 42}

_MON = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
        "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
        "listopada": 11, "grudnia": 12, "wrzesnia": 9, "pazdziernika": 10}

# statusy w kolumnie "głos" -> kategoria / pominięcie
_STATUS_MAP = {
    "za": "za",
    "przeciw": "przeciw",
    "wstrzymał się": "wstrzymal_sie",
    "wstrzymała się": "wstrzymal_sie",
    "nieobecny": None,
    "nieobecna": None,
    "brak": None,
    "nie głosował": None,
    "nie głosowała": None,
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_LAST = [0.0]


def _rate():
    now = time.time()
    wait = REQ_DELAY - (now - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()


def _fetch(url, binary=False, timeout=60):
    import urllib.request
    _rate()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 1. Discovery: 61710 → kadencja IX → lata → sesje → wykaz PDF
# ---------------------------------------------------------------------------
def discover_sessions():
    html = _fetch(ENTRY_URL)
    m = re.search(r'<a href="/public/\?id=(\d+)"[^>]*class="nazwa_pliku[^"]*"[^>]*>\s*Kadencja 2024-2029', html, re.I)
    if not m:
        raise RuntimeError("brak 'Kadencja 2024-2029' w kategorii sesji")
    kad_url = f"{BASE}/public/?id={m.group(1)}"
    kad_html = _fetch(kad_url)
    years = re.findall(
        r'<a href="(/public/\?id=\d+)"[^>]*class="nazwa_pliku[^"]*"[^>]*>\s*(\d{4}) rok', kad_html)
    sessions = {}   # (session_id) -> {"id","label","roman_num"}
    for href, year in years:
        yhtml = _fetch(BASE + href)
        for sm in re.finditer(
                r'<a href="/public/\?id=(\d+)"[^>]*class="nazwa_pliku[^"]*"[^>]*>\s*([^<]+?)\s*</a>', yhtml, re.S):
            sid, label = sm.group(1), re.sub(r"\s+", " ", sm.group(2)).strip()
            if "sesj" not in label.lower():
                continue
            if sid in sessions:
                continue
            rm = re.match(r"^([IVXL]+)\b", label)
            sessions[sid] = {"id": sid, "label": label, "number": _ROMAN.get(rm.group(1)) if rm else None}
    return [sessions[k] for k in sorted(sessions, key=lambda k: int(k))]


def wykaz_pdf_url(session_id):
    html = _fetch(f"{BASE}/public/?id={session_id}")
    cands = re.findall(r'<a href="(/public/getFile\?id=\d+)"[^>]*>.*?<span>([^<]+)</span>', html, re.S)
    hint = [f"{BASE}{h}".replace("&amp;", "&") for h, lab in cands
            if "wykaz" in lab.lower() or "głosowań" in lab.lower()]
    if hint:
        return hint[0]
    return None


# ---------------------------------------------------------------------------
# 2. Parser wykazu DSSS posiedzenia.pl (tekst)
# ---------------------------------------------------------------------------
def _pl_date(raw):
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", raw)
    if not m:
        return None
    mon = _MON.get(m.group(2).lower())
    if not mon:
        return None
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"


def _clean_topic(t):
    t = t.replace("\x00", " ").replace("#", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.strip().rstrip(",")
    t = re.sub(r"^druk nr \d+\s*[–-]\s*", "", t)
    return t.strip()


def parse_doc(doc, session_no):
    full = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    votes = []
    # bloki per głosowanie: od 'Wyniki imienne' do następnego nagłówka/końca
    # agregaty występują TUŻ PRZED 'Wyniki imienne'
    agg_re = re.compile(
        r"ZA\n(\d+)\n\d+(?:[.,]\d+)? ?%\s*(?:pula głosów\n\d+\n-\n)?.*?"
        r"PRZECIW\n(\d+)\n\d+(?:[.,]\d+)? ?%.*?"
        r"WSTRZYMA[ŁL]O SIĘ\n(\d+)\n\d+(?:[.,]\d+)? ?%",
        re.S)
    topic_re = re.compile(r"(?:głosowanie\n+|#? ?druk nr \d+)\n?#?\s*druk nr \d+\s*[–-]\s*(.+?)(?:\njednostka)", re.S)
    date_re = re.compile(r"data\n(\d{1,2} \w+ \d{4})")

    blocks = re.split(r"Wyniki imienne\n", full)
    # każdy blok po pierwszym zaczyna się od 'lp\nnazwisko\nimię\ngłos\n'
    head = blocks[0]
    for bi, block in enumerate(blocks[1:]):
        agg = None
        # znajdź ostatni agregat w poprzednim tekście (aggregate sits at end of the pre-list block)
        pre = blocks[bi]  # poprzedni kawałek (nagłówki + agregat)
        ams = list(agg_re.finditer(pre))
        if ams:
            m = ams[-1]
            agg = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        tms = list(topic_re.finditer(pre))
        topic = _clean_topic(tms[-1].group(1)) if tms else ""
        # data głosowania
        dmatch = re.search(r"data\n(\d{1,2} \w+ \d{4})", pre)
        vdate = _pl_date(dmatch.group(1)) if dmatch else None

        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        seq = re.findall(
            r"(\d{1,2})\n([A-ZŁŚŻŹĆ][^0-9\n]{1,40}?)\n([A-ZŁŚŻŹĆa-ząęółśżźć-]{3,25})\n"
            r"(ZA|PRZECIW|WSTRZYMA[ŁL]O SIĘ|WSTRZYMA[ŁL]A SIĘ|WSTRZYMA[ŁL] SIĘ|nieobecny|nieobecna|brak|BRAK|nie głosował|nie głosowała)\n",
            block, re.S)
        for _lp, surname, given, status_raw in seq:
            cat = _STATUS_MAP.get(status_raw.strip().lower(), None)
            if cat is None:
                continue
            name = f"{given.title()} {surname.strip()}"
            named[cat].append(name)
        if not agg:
            continue
        counts = {"za": len(named["za"]), "przeciw": len(named["przeciw"]),
                  "wstrzymal_sie": len(named["wstrzymal_sie"])}
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if agg != got:
            votes.append({"topic": topic, "named": None, "counts": counts, "agg": agg,
                          "session_date": vdate, "reconciled": False})
            continue
        votes.append({"topic": topic, "named": named, "counts": counts, "agg": agg,
                      "session_date": vdate, "reconciled": True, "session_number": session_no})
    return votes, full


# ---------------------------------------------------------------------------
# 3. Wyjscie
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o",
            "ś": "s", "ź": "z", "ż": "z"}
    slug = name.lower()
    for pl_, a in repl.items():
        slug = slug.replace(pl_, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, session_map):
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": session_map.get(d, d), "vote_count": 0}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": session_map.get(d, d),
            "topic": rec.get("topic", ""), "named_votes": rec["named"],
            "counts": rec["counts"],
        })
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        att = set()
        for v in all_votes:
            if v["session_date"] != d:
                continue
            for names in v["named_votes"].values():
                att.update(names)
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(att), "attendees": sorted(att), "speakers": [],
        })
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    # walidacja rosteru (stats vs skład)
    extra = all_names - set(_ROSTER)
    missing = set(_ROSTER) - all_names
    if extra or missing:
        print(f"  [warn] roster mismatch: extra={sorted(extra)} missing,{sorted(missing)}")
    stats = {n: defaultdict(int) for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                stats[nm][f"votes_{cat}" if cat != "wstrzymal_sie" else "votes_wstrzymal"] += 1
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    total_votes, total_sessions = len(all_votes), len(sessions_data)
    councilors_list = []
    for nm in sorted(all_names):
        st = stats[nm]
        present = st["votes_za"] + st["votes_przeciw"] + st["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(nm, set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": nm, "club": "", "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": st["votes_za"], "votes_przeciw": st["votes_przeciw"],
            "votes_wstrzymal": st["votes_wstrzymal"], "votes_brak": 0,
            "votes_nieobecny": 0, "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })
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
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "sessions": set()})
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START or not rec.get("named"):
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["sessions"].add(d)
    n_sess_with_votes = len({rec["session_date"] for rec in records
                             if rec.get("session_date") and (rec["session_date"] >= KAD_START)})
    n_records = max(1, sum(1 for rec in records if rec.get("named") and (rec.get("session_date") or "") >= KAD_START))
    profiles = []
    for nm in sorted(cv):
        vd = cv[nm]
        sess = len(vd["sessions"])
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_records * 100
        profiles.append({
            "name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                          "has_activity_data": False,
                          "frekwencja": round(sess / max(1, n_sess_with_votes) * 100, 1),
                          "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                          "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                          "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                          "votes_nieobecny": 0, "votes_total": vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"],
                          "rebellion_count": 0, "rebellions": [], "roles": [],
                          "notes": "", "former": False, "mid_term": False}},
        })
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        (out_path.parent / f"kadencja-{kid}.json").write_text(
            json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out_path.write_text(json.dumps({"generated": output.get("generated", ""),
                                    "default_kadencja": output.get("default_kadencja", ""),
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--limit", type=int, default=None, help="tylko N sesji (test)")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    sessions = discover_sessions()
    if args.limit:
        sessions = sessions[-args.limit:]
    print(f"[polczyn-zdroj] sesje IX kad. wykryte: {len(sessions)}")
    records, session_map, ok, skipped_votes = [], {}, 0, 0
    for s in sessions:
        url = wykaz_pdf_url(s["id"])
        if not url:
            print(f"  [skip] {s['label']}: brak 'Wykaz głosowań'")
            continue
        try:
            data = _fetch(url, binary=True)
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            print(f"  [ERR] {s['label']}: {e}")
            continue
        votes, _ = parse_doc(doc, s.get("number"))
        good = [v for v in votes if v.get("reconciled")]
        bad = len(votes) - len(good)
        skipped_votes += bad
        for v in good:
            d = v.get("session_date")
            if d and d not in session_map and s.get("number"):
                session_map[d] = s["number"]
        records.extend(good)
        ok += len(good)
        print(f"  [ok] {s['label']:45s} glosowan={len(good):3d} skip={bad} date={good[0]['session_date'] if good else '-'}")
    print(f"[polczyn-zdroj] razem glosowan zdanych: {ok} (pominięte nereconcilowane: {skipped_votes})")

    output = build_output(records, session_map)
    profiles = build_profiles(records)
    save_split(output, docs / "data.json", profiles)
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[polczyn-zdroj] zapisano: data.json, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
