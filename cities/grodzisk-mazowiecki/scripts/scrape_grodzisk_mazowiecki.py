#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Grodzisk Mazowiecki — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP Grodzisk Mazowiecki (bip.grodzisk.pl, platforma Madkom / Splay — React SPA
z REST API `{origin}/api/`). Drzewo: Organy(407) -> Rada Miejska(1812) ->
Sesje Rady Miejskiej(1857) -> "Imienne wyniki głosowań jawnych"(3131) -> "IX kadencja"(4974)
-> 26 artykułów "Wyniki imienne głosowań z sesji w dniu {DD.MM.YYYY}".
Każdy artykuł ma załącznik PDF (w API jako attachments[].id, link `e,pobierz,get.html?id=`),
a każdy PDF zawiera per-punkt-tabelę imienną:

    XXXII Sesja Rady Miejskiej w Grodzisku Mazowiecki
    Punkt 2 Przyjęcie porządku dziennego obrad
    Indywidualne wyniki uczestników:
    Nazwa uczestnika Grupa Odpowiedzi Frekwencja
    Magdalena Adamczyk Za Obecny(a)
    ...

Format czysto tekstowy (pdfplumber bez OCR). Odpowiedzi: Za / Przeciw / Wstrzymał(a) się
/ Nie głosował(a).

Zakres: 25 sesji (2024-05-15 .. 2026-07-29), 363 głosowania, 21 radnych.
I sesja (inauguracyjna 2024-05-07) ma INNY, zwarty layout (dwie osoby/wiersz "NazwiskoImię ZA"
+ bloki wyborów przewodniczącego tylko z liczbami zagregowanymi) — pominięta (jak I sesja w Krosnie).

Uwaga: radna Julia Gąsińska (2024-2025) -> Julia Gąsińska-Szewczyk (od 2025-07) — zmiana nazwiska
w trakcie kadencji; normalizowana do jednej kanonicznej formy.

Użycie:
    python scrape_grodzisk_mazowiecki.py --output docs/data.json --profiles docs/profiles.json
                                          [--config config.json] [--cache-dir DIR]
"""

import argparse
import io
import json
import re
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber

BASE = "https://bip.grodzisk.pl"
IMIENNE_MENU = "4974"  # "Imienne wyniki głosowań jawnych / IX kadencja"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0; +https://radoskop.eu)"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Kanoniczna lista radnych IX kadencji (wykaz ślubowania BIP). 21 osób.
ROSTER = ["Magdalena Adamczyk", "Joanna Apswoude", "Ewa Burzyk", "Irmina Dziekańska",
          "Robert Dziekański", "Piotr Galiński", "Julia Gąsińska-Szewczyk",
          "Jarosław Józefowicz", "Michał Klonowski", "Łukasz Lewandowski", "Urszula Misiło",
          "Łukasz Nowacki", "Bartłomiej Okurowski", "Janusz Okurowski", "Sylwester Stankiewicz",
          "Tomasz Suchożebrski", "Sylwia Śliwińska", "Dariusz Świderski", "Joanna Wiśniewska",
          "Joanna Wróblewska", "Luiza Złotkowska"]
ROSTER_SET = set(ROSTER)
# Mid-term nazwisko: Gąsińska -> Gąsińska-Szewczyk (od 2025-07)
ALIASES = {"Julia Gąsińska": "Julia Gąsińska-Szewczyk"}
RTOK = {"".join(c for c in unicodedata.normalize("NFKD", r) if not unicodedata.combining(c)).lower(): r
        for r in ROSTER}


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def _resolve_name(raw):
    raw = raw.strip()
    if not raw:
        return None
    if raw in ALIASES:
        raw = ALIASES[raw]
    if raw in ROSTER_SET:
        return raw
    k = _norm(raw)
    if k in RTOK:
        return RTOK[k]
    parts = raw.split()
    if len(parts) >= 2:
        rk = _norm(" ".join(reversed(parts)))
        if rk in RTOK:
            return RTOK[rk]
    # usuń nawiasowe sufiksy jak "Julia Gąsińska-Szewczyk (przew.)"
    return None


VOTE_MAP = {"Za": "za", "Przeciw": "przeciw", "Wstrzymał(a) się": "wstrzymal_sie",
            "Wstrzymal(a) się": "wstrzymal_sie", "Nie głosował(a)": None,
            "Nie glosowal(a)": None}


def _fetch(url, cache_dir, timeout=50, binary=False):
    if cache_dir:
        name = urllib.parse.quote(url, safe="") + (".bin" if (binary or url.endswith("action=download") or "pobierz" in url) else ".html")
        fp = Path(cache_dir) / name
        if fp.exists():
            return fp.read_bytes()
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
            data = r.read()
            if cache_dir:
                fp = Path(cache_dir) / name
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            return data
        except Exception as e:
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url)


def _api(mid, cache_dir, params=None):
    url = f"{BASE}/api/menu/{mid}/articles" + ("?" + urllib.parse.urlencode(params) if params else "")
    data = _fetch(url, cache_dir)
    return json.loads(data.decode("utf-8", "replace"))


def _api_article(aid, cache_dir):
    url = f"{BASE}/api/articles/{aid}"
    data = _fetch(url, cache_dir)
    return json.loads(data.decode("utf-8", "replace"))


def _harvest(cache_dir):
    """26 artykułów imiennych IX kadencji -> [{date, aid, att_id, title}]."""
    out = []
    off = 0
    while True:
        d = _api(IMIENNE_MENU, cache_dir, {"offset": off, "limit": 100})
        lst = d.get("articles") or []
        for it in lst:
            a = it.get("aliasFields") or []
            title = next((x["value"] for x in a if x["alias"] == "title"), "")
            title = re.sub(r"<[^>]+>", "", title or "")
            m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", title)
            if not m:
                continue
            date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            # attachment id z artykułu
            att = None
            try:
                ad = _api_article(it["id"], cache_dir)
                atts = ad.get("attachments") or []
                if atts:
                    att = str(atts[0]["id"])
            except Exception:
                pass
            out.append({"aid": it["id"], "date": date, "att": att, "title": title})
        total = d.get("total", 0)
        off += len(lst)
        if len(lst) < 100 or off >= total:
            break
    out = [s for s in out if s.get("date", "") >= "2024-05-07"]
    # opcjonalnie fallback: atrybucja attachment z content regex
    for s in out:
        if s.get("att"):
            continue
        try:
            ad = _api_article(s["aid"], cache_dir)
            mm = re.search(r"(?:Download/get/id,|pobierz,get\.html\?id=)(\d+)", ad.get("content") or "")
            if mm:
                s["att"] = mm.group(1)
        except Exception:
            pass
    out.sort(key=lambda x: x["date"])
    return out


def _raw_lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [ln.rstrip() for pg in pdf.pages for ln in (pg.extract_text() or "").split("\n")]


ROW_RE = re.compile(r"^(.*?)\s+((?:Za|Przeciw|Wstrzymał\(a\) się|Wstrzymal\(a\) się|Nie głosował\(a\)|Nie glosowal\(a\)))\s+(?:Obecny\(a\)|Nieobecny\(a\)|Obecn.)\s*$")


def _parse_pdf(data):
    """Bloki głosowań: [{topic, votes:[(name, cat_or_None)]}] — standardowy format."""
    lines = _raw_lines(data)
    # podziel na segmenty wg "Indywidualne wyniki uczestników:"
    segs, cur = [], []
    for ln in lines:
        if ln.strip() == "Indywidualne wyniki uczestników:":
            segs.append(cur)
            cur = []
        else:
            cur.append(ln)
    if any(x.strip() for x in cur):
        segs.append(cur)
    blocks = []
    for seg in segs:
        hi = None
        for j, ln in enumerate(seg):
            if "Nazwa uczestnika" in ln and "Odpowiedzi" in ln:
                hi = j
                break
        if hi is None:
            continue
        hdr_lines = seg[:hi]
        # usuń nagłówek sesji: "XXXII Sesja Rady Miejskiej w Grodzisku" / "Mazowiecki"
        hdr = [l for l in hdr_lines
               if not re.match(r"^\s*[IVXL]+\s+Sesja\b", l) and l.strip() != "Mazowiecki"]
        topic = re.sub(r"\s+", " ", " ".join(x.strip() for x in hdr)).strip()
        rows = []
        for ln in seg[hi + 1:]:
            m = ROW_RE.match(ln.strip())
            if m:
                nm = _resolve_name(m.group(1))
                if nm:
                    cat = VOTE_MAP.get(m.group(2))
                    rows.append((nm, cat))
        if topic or rows:
            blocks.append({"topic": topic, "votes": rows})
    return blocks


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
          "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
          "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
          "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
          "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32}
_ROMAN_REV = {v: k for k, v in _ROMAN.items()}


def _club_of(name):
    return ""


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def _run(cache_dir, date2roman=None):
    sessions = _harvest(cache_dir)
    records = []
    for s in sessions:
        if s["date"] == "2024-05-07":  # inauguracyjna — inny layout, agregaty-only
            continue
        if not s.get("att"):
            raise RuntimeError(f"brak attachment dla sesji {s['date']}")
        url = f"{BASE}/e,pobierz,get.html?id={s['att']}"
        data = _fetch(url, cache_dir, binary=True)
        blocks = _parse_pdf(data)
        if not blocks:
            print(f"  [grodzisk] ostrzeżenie: sesja {s['date']} — 0 bloków głosowań")
        for b in blocks:
            records.append({"date": s["date"], "title": s["title"],
                            "number": (date2roman or {}).get(s["date"], ""),
                            "topic": b["topic"], "votes": b["votes"]})
    return records


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("number", ""),
                                   "vote_count": 0, "attendees": set()}
        named = defaultdict(list)
        for nm, cat in rec["votes"]:
            if cat:
                named[cat].append(nm)
        named = dict(named)
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d,
            "session_number": sessions_by_date[d]["number"],
            "topic": rec["topic"] or rec["title"] or "",
            "named_votes": dict(named),
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })
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
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": _club_of(name), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors_data.get(name)
                if not c:
                    continue
                c["votes_za"] += cat == "za"
                c["votes_przeciw"] += cat == "przeciw"
                c["votes_wstrzymal"] += cat == "wstrzymal_sie"

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nme in names:
                councillor_sess[nme].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})

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
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(x) for x in all_names)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        for nm, cat in rec["votes"]:
            if not cat:
                continue
            cv[nm][cat] += 1
            cv[nm]["votes"].append({"session": d, "vote": cat})
    all_sess = {rec["date"] for rec in records}
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        present_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / len(all_sess) if all_sess else 0.0
        profiles.append({
            "name": name, "slug": _slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw, 1), "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"], "votes_wstrzymal": vd["wstrzymal_sie"],
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles}


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
             "default_kadencja": output.get("default_kadencja", ""), "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="docs/data.json")
    ap.add_argument("--profiles", default="docs/profiles.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    cache_dir = args.cache_dir
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

    # data -> rzymski numer sesji (z protokołów IX kadencji menu 4983)
    date2roman = {}
    try:
        djson = _fetch(f"{BASE}/api/menu/4983/articles?offset=0&limit=200", cache_dir)
        d = json.loads(djson.decode("utf-8", "replace"))
        _MON = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
                "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}
        for it in d.get("articles", []):
            a = it.get("aliasFields") or []
            title = re.sub(r"<[^>]+>", "", next((x["value"] for x in a if x["alias"] == "title"), ""))
            m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", title.lower())
            r = re.match(r"Protokół\s*([IVXL]+)", title)
            if m and r:
                mo = _MON.get(m.group(2))
                if mo:
                    date2roman[f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"] = r.group(1)
    except Exception as e:
        print(f"  [grodzisk] brak mapy numerów sesji: {e}")

    records = _run(cache_dir, date2roman)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    kad = output["kadencje"][0]
    print(f"  Grodzisk Mazowiecki: {kad['total_sessions']} sesji, {kad['total_votes']} głosowań, "
          f"{kad['total_councilors']} radnych")
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
