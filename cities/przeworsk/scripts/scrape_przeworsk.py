#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Przeworsk — imienne głosowania Rady Miasta Przeworska (IX kadencja).

Źródło: BIP przeworsk.bip.info.pl (platforma bip.info.pl, kategorie index.php?idmp=N).
Struktura: menu przedmiotowe 'Uchwały, Zarządzenia i Protokoły Rady Miasta' (idmp=1)
→ 'Kadencja 2024-2029' (idmp=827) → per sesja kategoria 'Sesja Rady Miasta
Przeworska - D miesiąca YYYY roku' (iddok lista dokumentów-uchwał). Każdy dokument
uchwały ma załącznik 'Wyniki głosowania radnych - Uchwała ...' (plik.php?id=N) =
PDF DSSS Vote App ze WARSTWĄ TEKSTOWĄ: agregat 'jestem za N, jestem przeciw N,
wstrzymuję się N' + listy imienne kolumnowe 'Jestem za / Jestem przeciw /
Wstrzymuję się / Obecni radni, którzy nie wzięli udziału' (BRAK = non-vote).
Rekonstrukcja pozycyjna kolumn (lewa x<300, prawa x>=300), per-vote walidacja
liczb imiennych vs agregat (nie reconciluje → pomiń, nie fabrykujemy).

Roster IX kad. (przeworsk.um.gov.pl/rada-miasta): 15 radnych, przewodniczący
Tomasz Majba, wiceprzewodniczący Marek Janisz i Elżbieta Cholewa.

Użycie: python scrape_przeworsk.py --city-dir cities/przeworsk [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
try:
    from lib_names_pl import fix_all as _fix_all_names
except Exception:  # pragma: no cover
    def _fix_all_names(names):
        return names

BIP = "https://przeworsk.bip.info.pl"
KAD_CAT_ID = 827  # 'Kadencja 2024-2029' w menu przedmiotowym
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.35
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
          "pazdziernika": 10, "listopada": 11, "grudnia": 12}

ROLES = {
    "Tomasz Majba": "Przewodniczący Rady Miasta Przeworska",
    "Marek Janisz": "Wiceprzewodniczący Rady Miasta Przeworska",
    "Elżbieta Cholewa": "Wiceprzewodniczący Rady Miasta Przeworska",
}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    if cache is not None:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    data = resp.content if binary else None
    if binary:
        out = resp.content
    else:
        resp.encoding = resp.apparent_encoding
        out = resp.text
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        cf.write_bytes(out.encode("utf-8", "ignore") if isinstance(out, str) else out)
    return out


def _unesc(s):
    import html as _h
    return _h.unescape(s)


def make_slug(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ------------------------------------------------------- discovery (mapa.php)
def discover_sessions(cache=None):
    """Kategorie sesji z mapa.php: 'Sesja Rady Miasta Przeworska - D miesiąca YYYY roku'."""
    html = _fetch(BIP + "/mapa.php", cache)
    links = re.findall(r"href='index\.php\?[^']*idmp=(\d+)[^']*'[^>]*>([^<]+)</a>", html)
    sessions = []
    for idmp, title in links:
        title = _unesc(title).strip()
        m = re.match(r"Sesja Rady Miasta Przeworska\s*-\s*(\d{1,2})\s+(\w+)\s+(\d{4})\s+roku", title)
        if not m:
            continue
        mon = MONTHS.get(m.group(2).lower().replace("ż", "z").replace("ź", "z"))
        # spróbuj najpierw oryginalnej pisowni
        mon = MONTHS.get(m.group(2).lower(), mon)
        if not mon:
            continue
        date = f"{m.group(3)}-{int(mon):02d}-{int(m.group(1)):02d}"
        sessions.append({"idmp": int(idmp), "date": date, "title": title})
    # dedup po idmp, zostaw IX kadencję
    seen = {}
    for s in sessions:
        seen.setdefault(s["idmp"], s)
    out = [s for s in seen.values() if s["date"] >= KAD_START]
    out.sort(key=lambda s: s["date"])
    return out


def session_number(cat_html):
    """Rzymski numer sesji z numerów uchwał (XXVIII/219/2026)."""
    nums = re.findall(r"Nr?\s+([IVXLVIX]+)?/?\d*", cat_html)
    m = re.findall(r"[Nn]r\s+([IVXLCDM]+)\/\d+\/\d{4}", cat_html)
    if m:
        return m[0]
    return ""


# ------------------------------------------------------- parsing PDF (DSSS)
def _col_lines(words, x_lo, x_hi, y_lo, y_hi):
    sel = [w for w in words if x_lo <= w[0] < x_hi and y_lo <= w[1] <= y_hi]
    sel.sort(key=lambda w: (round(w[1] / 6), w[0]))
    lines = defaultdict(list)
    for w in sel:
        lines[round(w[1] / 6)].append((w[0], w[4]))
    return [" ".join(t for _, t in sorted(lines[k], key=lambda z: z[0])) for k in sorted(lines)]


def _parse_lists(lines):
    cat, cats = None, defaultdict(list)
    for ln in lines:
        low = ln.lower()
        if "jestem przeciw" in low:
            cat = "przeciw"
        elif "jestem za" in low:
            cat = "za"
        elif "wstrzymuj" in low:
            cat = "wstrzym"
        elif "obecni radni" in low or "nie wzięli" in low or low.startswith("udziału") or low.startswith("w głosowaniu"):
            cat = "obecni_no"
        elif cat and re.match(r"^\d+\.\s+[A-ZŁŚŻŃĆ]", ln):
            cats[cat].append(re.sub(r"^\d+\.\s+", "", ln).strip())
    return cats


def parse_wyniki_pdf(pdf_bytes):
    """Zwraca listę głosowań (zwykle 1 na PDF). None jeśli brak tabeli."""
    out = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    for i in range(doc.page_count):
        pg = doc[i]
        t = " ".join(pg.get_text().split())
        m = re.search(r"jestem za\s*(\d+),\s*jestem przeciw\s*(\d+),\s*wstrzymuję się\s*(\d+)", t, re.I)
        if not m:
            continue
        agg = tuple(int(x) for x in m.groups())
        words = pg.get_text("words")
        y_lo = 250.0
        for w in words:
            if w[4] == "zagłosowali":
                y_lo = w[1] - 20
                break
        left = _col_lines(words, 0, 300, y_lo, 860)
        right = _col_lines(words, 300, 600, y_lo, 860)
        lc, rc = _parse_lists(left), _parse_lists(right)
        named = {"za": _fix_all_names(lc["za"] + rc["za"]),
                 "przeciw": _fix_all_names(lc["przeciw"] + rc["przeciw"]),
                 "wstrzymal_sie": _fix_all_names(lc["wstrzym"] + rc["wstrzym"])}
        got = (len(named["za"]), len(named["przeciw"]), len(named["wstrzymal_sie"]))
        if agg != got:
            continue  # nie reconciluje → pomiń
        tm = re.search(r"[Uu]chwała\s+numer\s+(\S+)\s+[“\"]?([^”\"]{5,180})", t)
        topic = ""
        number = ""
        if tm:
            number = tm.group(1)
            topic = tm.group(2).strip().rstrip("”\"")
        dm = re.search(r"Data i godzina głosowania:\s*(\d{4}-\d{2}-\d{2})", t)
        vdate = dm.group(1) if dm else ""
        absent = re.search(r"ustawowego składu rady|bezwzgl", t, re.I)
        out.append({"topic": topic or number, "resolution_no": number,
                    "named": named, "counts": {"za": got[0], "przeciw": got[1], "wstrzymal_sie": got[2]},
                    "session_date": vdate})
    return out


def session_votes(idmp, cache=None):
    """Wszystkie 'Wyniki głosowania radnych' z kategorii sesji."""
    html = _fetch(f"{BIP}/index.php?r=r&idmp={idmp}", cache)
    docs = re.findall(r"dokument\.php\?iddok=(\d+)&amp;idmp=\d+[^']*'\s*>(.*?)</a>", html, re.S)
    number = session_number(html)
    votes = []
    for idd, _raw in docs:
        dh = _fetch(f"{BIP}/dokument.php?iddok={idd}&idmp={idmp}&r=r", cache)
        i = dh.find("doc-attachments")
        seg = dh[i:] if i >= 0 else dh
        atts = re.findall(r"<li><a href='(plik\.php\?id=\d+[^']*)'>(.*?)</a>", seg)
        for href, at in atts:
            at = _unesc(at).strip()
            if "Wyniki głosowania" not in at:
                continue
            try:
                pdf = _fetch(BIP + "/" + href, cache, binary=True)
                vs = parse_wyniki_pdf(pdf)
            except Exception as e:
                print(f"  [ERR pdf dok={idd}] {e}")
                continue
            for v in vs:
                v["source_url"] = BIP + "/dokument.php?iddok=" + idd + f"&idmp={idmp}&r=r"
                votes.append(v)
    return number, votes


# ------------------------------------------------------- output
def build_output(records, session_meta):
    sessions_by_date = {}
    all_votes = []
    vid = 0
    for rec in records:
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": session_meta.get(d, {}).get("number", d),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": session_meta.get(d, {}).get("number", d),
                          "topic": rec.get("topic", ""), "title": rec.get("topic", ""),
                          "named_votes": rec["named"], "counts": rec["counts"],
                          "source_url": rec.get("source_url", "")})
    sessions_data = [{"date": d, "number": sessions_by_date[d]["number"],
                      "vote_count": sessions_by_date[d]["vote_count"],
                      "attendee_count": len(sessions_by_date[d]["attendees"]),
                      "attendees": sorted(sessions_by_date[d]["attendees"]), "speakers": []}
                     for d in sorted(sessions_by_date)]
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    cc = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                key = "wstrzymal" if cat == "wstrzymal_sie" else cat
                cc[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    c_session = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                c_session[nm].add(v["session_date"])
    councilors_list = []
    for nm in sorted(cc):
        present = sum(cc[nm].values())
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(c_session[nm]) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({
            "name": nm, "club": "", "district": None, "role": ROLES.get(nm, ""),
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc[nm]["za"], "votes_przeciw": cc[nm]["przeciw"],
            "votes_wstrzymal": cc[nm]["wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None,
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
        same = sum(1 for vid2 in common if vectors[a][vid2] == vectors[b][vid2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}, kad


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    sess = defaultdict(set)
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                sess[nm].add(d)
    n_sessions = len({r.get("session_date") for r in records if r.get("session_date", "") >= KAD_START}) or 1
    total_records = sum(1 for r in records if r.get("session_date", "") >= KAD_START)
    profiles = []
    for nm in sorted(cv):
        vd = cv[nm]
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, total_records) * 100
        frekwencja = len(sess[nm]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": "", "role": ROLES.get(nm, ""),
                             "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywn, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": sum(vd.values()),
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"scraped_at": datetime.now().isoformat(), "profiles": profiles, "total": len(profiles)}


def save_split(data, kad, out_dir, profiles):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[przeworsk] kategorie sesji IX kad.: {len(sessions)}")
    records = []
    session_meta = {}
    for s in sessions:
        number, votes = session_votes(s["idmp"], cache)
        session_meta[s["date"]] = {"number": number}
        for v in votes:
            if not v["session_date"]:
                v["session_date"] = s["date"]
            records.append(v)
        print(f"  {s['date']} sesja {number}: votes={len(votes)}")
    data, kad = build_output(records, session_meta)
    profiles = build_profiles(records)
    save_split(data, kad, city_dir / "docs", profiles)
    print(f"[przeworsk] total votes={kad['total_votes']} sessions={kad['total_sessions']} "
          f"councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
