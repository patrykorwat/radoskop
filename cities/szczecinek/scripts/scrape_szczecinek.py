#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Szczecinek — imienne głosowania Rady Miasta Szczecinek (IX kadencja).

Źródło: BIP bip.szczecinek.pl (custom CMS "artykuly/uchwaly/attachments").
Kategoria uchwał: https://bip.szczecinek.pl/uchwaly/477 (paginacja /{page}/10).
Każda uchwała ma załącznik "glosowanie <NNN>" = wydruk eSesja per głosowanie
(PDF, tabela dwukolumnowa "Lp | Nazwisko i imię | Głos": ZA / PRZECIW /
WSTRZYMUJĘ SIĘ / NIEOBECNY(A) / OBECNY(A)). Header PDF zawiera nazwę sesji
("XXXVIII Sesja IX Kadencji Rady Miasta Szczecinek") i "Data głosowania:
DD.MM.YYYY HH:MM". Walidacja per głos: liczone głosy imienne == agregaty.

Protokoły z sesji (/artykuly/207) są NARRACYJNE ("Wykaz głosowania imiennego
stanowi załącznik do podjętej uchwały") — jedyne imienne dane to załączniki
per-uchwała. Parser tabeli wzorowany na cities/goleniow (wydruk eSesja).

Użycie:
    python scrape_szczecinek.py --city-dir <cities/szczecinek> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
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

BIP = "https://bip.szczecinek.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.5
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        (Path(cache_dir) / (key + ".dat")).write_bytes(data)
    return data


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _norm_vote(w):
    k = _nk(w)
    if k in ("za", "z", "ża", "ze"):
        return "za"
    if k.startswith("przeciw"):
        return "przeciw"
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("nieobecn"):
        return "nieobecni"
    if k.startswith("obecn"):
        return "obecny"
    return None


_ROM = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
        "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17,
        "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24,
        "XXV": 25, "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31,
        "XXXII": 32, "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36, "XXXVII": 37,
        "XXXVIII": 38, "XXXIX": 39, "XL": 40, "XLI": 41, "XLII": 42, "XLIII": 43, "XLIV": 44,
        "XLV": 45, "XLVI": 46, "XLVII": 47, "XLVIII": 48, "XLIX": 49, "L": 50, "LI": 51,
        "LII": 52, "LIII": 53, "LIV": 54, "LV": 55, "LVI": 56, "LVII": 57, "LVIII": 58, "LIX": 59,
        "LX": 60, "LXI": 61, "LXII": 62, "LXIII": 63, "LXIV": 64, "LXV": 65}


# ---------------- discovery ----------------
def discover_uchwaly(cache_dir):
    """Zwraca listę artykułów uchwał: {'url','slugnum'} (kategoria 477, paginacja)."""
    arts = []
    seen = set()
    page = 1
    while page <= 120:
        url = f"{BIP}/uchwaly/477/{page}/10" if page > 1 else f"{BIP}/uchwaly/477"
        try:
            t = _get(url, cache_dir).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  [discover] page {page} ERR {e}")
            break
        urls = re.findall(r'href="(https://bip\.szczecinek\.pl/uchwala/\d+/[^"]+)"', t)
        new = [u for u in urls if u not in seen]
        if not new:
            break
        seen.update(new)
        arts.extend(new)
        page += 1
    return arts


def article_vote_attachments(html):
    """Znajdź attachment 'glosowanie NNN' (per-vote wydruk eSesja) + numer sesji/rok z nagłówka."""
    out = []
    for m in re.finditer(r'href="((?:https://bip\.szczecinek\.pl)?/attachments/download/\d+)"[^>]*>\s*([^<]{1,60}?)\s*<', html, re.S):
        href, label = m.group(1), m.group(2).strip()
        if re.search(r'(?i)g[łl]osowanie', label):
            out.append(href if href.startswith("http") else BIP + href)
    return out


# ---------------- PDF parsing (wydruk eSesja, dwukolumnowa tabela) ----------------
# Dwa warianty layoutu (per reference esesja-printout-pdf-parsing.md):
#  - plain: caly wiersz w jednej linii extract_text()
#  - form-fill ("edytowalny"): kazde slowo to osobne pole -> extract_text()
#    rozrzuca slowa; trzeba isc po extract_words() + kotwiczenie po tokenach glosu.
# Parser ponizszy jest wylacznie slowo-wspołrzednosciowy -> dziala na obu.

def _vote_tok(text):
    """Token glosu musi byc ALL-CAPS (etykiety naglowka sa mix-case)."""
    if text.upper() != text:
        return None
    k = _nk(text)
    if k == "za":
        return "za"
    if k.startswith("przeciw"):
        return "przeciw"
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("nieobecn"):
        return "nieobecni"
    if k.startswith("obecn"):
        return "obecny"
    return None


def _table_words(words):
    up = [w for w in words if _nk(w["text"]) == "uprawnieni"]
    if not up:
        return words
    thr = max(w["top"] for w in up)
    return [w for w in words if w["top"] > thr + 4]


def _extract_cells(words):
    """Kotwiczenie po tokenach glosu: nazwisko = slowa miedzy Lp wiersza a tokenem."""
    votes = [w for w in words if _vote_tok(w["text"]) and re.search(r"[A-Z]{2}", w["text"])]
    lps = [w for w in words if re.match(r"^\d{1,2}\.$", w["text"])]
    cells = []
    for v in votes:
        band = [w for w in lps if abs((w["top"] + w["bottom"]) / 2 - (v["top"] + v["bottom"]) / 2) < 7 and w["x0"] < v["x0"]]
        if not band:
            continue
        lp = max(band, key=lambda w: w["x0"])
        lo, hi = lp["x0"], v["x0"] - 4
        win = 8
        name_ws = [w for w in words
                   if lo <= (w["x0"] + w["x1"]) / 2 <= hi
                   and abs((w["top"] + w["bottom"]) / 2 - (v["top"] + v["bottom"]) / 2) < win
                   and not re.match(r"^\d{1,2}\.$", w["text"])
                   and _nk(w["text"]) not in ("sie", "się")
                   and _vote_tok(w["text"]) is None
                   and _nk(w["text"]) not in ("lp", "nazwisko", "imie", "imię", "glos", "głos")]
        name_ws.sort(key=lambda w: w["x0"])
        name = " ".join(w["text"] for w in name_ws).strip()
        if name:
            cells.append((name, _vote_tok(v["text"])))
    return cells


def _extract_aggs(words, text):
    """Agregaty — slowo-wspołrzednosciowo (form-fill rozbija linii w extract_text)."""
    agg = {}
    def num_after(label_words, x_max=None):
        # znajdz ostatnie wystapienie etykiety (senq wyrazowa), wez pierwsza cyfre na prawo
        for i in range(len(words) - len(label_words) + 1):
            seq = words[i:i + len(label_words)]
            tops = {round(w["top"]) for w in seq}
            if len(tops) > 1:
                continue
            if [_nk(w["text"]) for w in seq] == label_words:
                right = [w for w in words
                         if abs((w["top"] + w["bottom"]) / 2 - (seq[0]["top"] + seq[0]["bottom"]) / 2) < 6
                         and w["x0"] >= seq[-1]["x1"] - 2
                         and re.fullmatch(r"\d+", w["text"])
                         and (x_max is None or w["x0"] < x_max)]
                if right:
                    return int(min(right, key=lambda w: w["x0"])["text"])
        return None
    left_x = None
    za_lbl = [w for w in words if _nk(w["text"]) == "za" and w["top"] < 300]
    # granica lewej kolumny licznikow = x tokena 'Głosy'
    glosy = [w for w in words if w["text"] == "Głosy"]
    if glosy:
        left_x = min(w["x0"] for w in glosy)
    v = num_after(["liczba", "uprawnionych"]);        agg["uprawnionych"] = v if v is not None else -1
    v = num_after(["liczba", "obecnych"]);            agg["obecnych"] = v if v is not None else -1
    v = num_after(["liczba", "nieobecnych"]);         agg["nieobecnych"] = v if v is not None else -1
    v = num_after(["obecni", "nieglosujacy"]);        agg["obecni_nieglosujacy"] = v if v is not None else -1
    v = num_after(["glosy", "za"], x_max=left_x);     
    if v is None: v = num_after(["glosy", "za"])
    agg["za"] = v if v is not None else -1
    v = num_after(["glosy", "przeciw"]);              agg["przeciw"] = v if v is not None else -1
    v = num_after(["glosy", "wstrzymujace", "sie"]);  agg["wstrzym"] = v if v is not None else -1
    # fallback tekstowy (plain layout)
    for key, pat in [("uprawnionych", r"Liczba uprawnionych\s+(\d+)"),
                     ("obecnych", r"Liczba obecnych\s+(\d+)"),
                     ("nieobecnych", r"Liczba nieobecnych\s+(\d+)"),
                     ("obecni_nieglosujacy", r"Obecni niegłosujący\s+(\d+)"),
                     ("za", r"Głosy za\s+(\d+)"),
                     ("przeciw", r"Głosy przeciw\s+(\d+)"),
                     ("wstrzym", r"Głosy wstrzymujące się\s+(\d+)")]:
        if agg.get(key, -1) == -1:
            m = re.search(pat, text)
            if m:
                agg[key] = int(m.group(1))
    return agg


def _extract_topic(words):
    """Miedzy tokenem 'Głosowanie' a 'Typ' (x0>50, poza numerem glosowania)."""
    g = [w for w in words if w["text"] == "Głosowanie"]
    ty = [w for w in words if _nk(w["text"]) == "typ"]
    if not g:
        return "(glosowanie)"
    gt = g[0]["top"]
    thr = min((w["top"] for w in ty if w["top"] > gt), default=gt + 60)
    ws = sorted((w for w in words if gt - 2 < (w["top"] + w["bottom"]) / 2 < thr and w["x0"] > 50),
                key=lambda w: (w["top"], w["x0"]))
    toks = [w["text"] for w in ws]
    topic = " ".join(t for t in toks if not re.match(r"^\d{1,2}\.?$", t))
    topic = re.sub(r"\s+", " ", topic).strip(" .:,;-")
    return topic or "(glosowanie)"


def parse_vote_pdf(data):
    """Jeden PDF = zwykle 1 głosowanie. Zwraca record albo None (głosowanie bezimienne)."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        votes = []
        cur = None
        for page in pdf.pages:
            words = page.extract_words()
            text = page.extract_text() or ""
            has_agg = "Liczba uprawnionych" in text
            tw = _table_words(words)
            cells = _extract_cells(tw)
            if has_agg:
                agg = _extract_aggs(words, text)
                topic = _extract_topic(words)
                sm = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
                date = None
                if sm:
                    d, mo, y = int(sm.group(1)), int(sm.group(2)), int(sm.group(3))
                    date = f"{y:04d}-{mo:02d}-{d:02d}"
                sess = re.search(r"(?i)(XXX[IVX]*|XL[IXV]*|XL|XX[XVIX]*|X[XVIX]*|IX|VIII|VII|VI|V|IV|III|II|I)\s+Sesja", text)
                num = sess.group(1).upper() if sess else ""
                cur = {"topic": topic, "agg": agg, "cells": cells, "date": date, "num": num}
                votes.append(cur)
            else:
                if cur is not None:
                    cur["cells"] = cur["cells"] + cells
        records = []
        for v in votes:
            counter = Counter(vote for _n, vote in v["cells"])
            ok = (counter.get("za", 0) == v["agg"].get("za", -1) and
                  counter.get("przeciw", 0) == v["agg"].get("przeciw", -1) and
                  counter.get("wstrzymal_sie", 0) == v["agg"].get("wstrzym", -1) and
                  counter.get("nieobecni", 0) == v["agg"].get("nieobecnych", -1) and
                  counter.get("obecny", 0) == v["agg"].get("obecni_nieglosujacy", -1))
            if not ok:
                continue
            named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
            for nm, vote in v["cells"]:
                nm = disp_name(nm)
                if nm and vote in named:
                    named[vote].append(nm)
            records.append({"topic": v["topic"], "named": named, "date": v["date"], "num": v["num"]})
        return records[0] if records else None


# display name: źródło "Nazwisko imię" -> Radoskop "Imię Nazwisko"
def disp_name(src):
    toks = src.split()
    if not toks:
        return ""
    return " ".join(reversed(toks))


def make_slug(name):
    repl = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z",
            "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z"}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0,
                                   "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
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
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
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
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
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
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-uchwaly", type=int, default=0)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    arts = discover_uchwaly(cache)
    print(f"[szczecinek] artykulow uchwal: {len(arts)}")
    if args.max_uchwaly:
        arts = arts[:args.max_uchwaly]

    records = []
    seen_att = set()
    no_votes = 0
    for a in arts:
        try:
            art = _get(a, cache).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  [ERR art {a}] {e}")
            continue
        atts = article_vote_attachments(art)
        if not atts:
            no_votes += 1
            continue
        for att in atts:
            if att in seen_att:
                continue
            seen_att.add(att)
            try:
                data = _get(att, cache)
                if not data[:4] == b"%PDF":
                    continue
                rec = parse_vote_pdf(data)
                if rec:
                    records.append(rec)
                else:
                    no_votes += 1
            except Exception as e:
                print(f"  [ERR pdf {att}] {type(e).__name__}: {e}")
    dated = [r for r in records if r["date"] and r["date"] >= KAD_START]
    print(f"[szczecinek] glosowania imienne: {len(records)} (w kadencji {len(dated)}), bez-imiennych/skip: {no_votes}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[szczecinek] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")


if __name__ == "__main__":
    main()
