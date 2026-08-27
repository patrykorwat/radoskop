#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Międzyrzecz — imienne głosowania Rady Miejskiej w Międzyrzeczu (IX kadencja 2024-2029).

Źródło: BIP bip.miedzyrzecz.pl, kategoria /309/Glosowania/ — 30 artykułów (sesje I…XXX, IX kad.)
tytuły "Wykaz imiennych głosowań przeprowadzonych podczas <N> sesji Rady Miejskiej w Międzyrzeczu
w dniu <dd.mm.rrrr> r. zapisany w formie edytowalnego pliku PDF". Dla KAŻDEJ sesji artykuł zawiera
jeden załącznik PDF (link /system/pobierz.php?plik=<N>-24-glosowanie.pdf&id=...) z WSZYSTKIMI imiennymi
głosowaniami tej sesji — wydruk eSesja, format standardowy, jeden głos na stronę (lub dłuższe na kilku).

Dwa warianty wizualne PDF (oba obsługiwane):
  * format "zwykły":  dwukolumnowa tabela  Lp | Nazwisko i imię | Głos  (ZA/PRZECIW/WSTRZYMUJE SIĘ/
    OBECNY/NIEOBECNY), nazwiska w jednej linii;
  * format "edytowalny" (form-fill, nowsze sesje: XXVI+): te same dane, ale każde słowo w osobnym polu
    formularza, więc extract_text zwraca słowa rozrzucone po liniach; parsowanie oparte o współrzędne.

Parser per głos (jeden głos = strona z nagłówkiem zawierającym "Kworum ..."):
  - nagłówek: Liczba uprawnionych / obecnych / nieobecnych / Obecni niegłosujący +
    Głosy za / przeciw / wstrzymujące się  (wartości liczbowe);
  - temat: słowa między "Głosowanie" a "Typ głosowania" (x0 > 50, bez numeru głosu);
  - tabela imienna: kotwica = tokeny głosu (ZA/PRZECIW/WSTRZYMUJE/SIĘ/OBECNY/OBECNA/NIEOBECNY),
    nazwisko = słowa w tej samej połowie strony, na lewo od tokenu, w oknie pionowym ±12 pt;
  - walidacja per głos: zliczone imienne ZA/PRZECIW/WSTRZYMUJE się/NIEOBECNY/OBECNY == zagregaty z nagłówka.

Skład Rady jest DYNAMICZNY (zmiany w trakcie kadencji: Bełz/Olender/Sawka → Gołębiewski/Hudziak/Kijak),
więc role radnych budowany jest jako pełny zbiór unikalnych imion+nazwisk z przeanalizowanych głosów.
Obrót nazwisk: źródło podaje "Nazwisko Imię", Radoskop wyświetla "Imię Nazwisko".

Wyjście (standard Radoskop): docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json

Użycie:
    python scrape_miedzyrzecz.py --city-dir <katalog z work/miedzyrzecz> [--cache-dir dir] [--skip-download]
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.miedzyrzecz.pl"
CATEGORY = "/309/Glosowania/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.5
_LAST = 0.0

_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
        "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
        "XXVIII":28,"XXIX":29,"XXX":30}

_VOTE_TOKENS = {"ZA", "PRZECIW", "PRZECIWNA", "WSTRZYMUJE", "OBECNY", "OBECNA", "OBECNI",
                "NIEOBECNY", "NIEOBECNA", "NIEOBECNI"}


def _is_vote_token(txt):
    """Zwraca kategorię głosu dla tokenu lub None. Uwzględnia formy żeńskie i męskie."""
    # tokeny głosu w tabeli są uppercase (ZA/PRZECIW/...); etykiety nagłówka lowercase
    if txt != txt.upper():
        return None
    k = _nk(txt)
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


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        (cache_dir / (key + ".dat")).write_bytes(data)
    return data


# ---------------- discovery ----------------
def discover_sessions(cache_dir=None):
    """Kategoria /309/Glosowania/ -> lista sesji {num,date,title,url} + link PDF."""
    t = _get(BIP + CATEGORY, cache_dir).decode("utf-8", "ignore")
    from html import unescape
    sessions = []
    seen = set()
    for m in re.finditer(r'<a href="(https://bip\.miedzyrzecz\.pl/309/(\d+)/[^"]+)"[^>]*>(.*?)</a>', t, re.S):
        url = m.group(1); aid = m.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        title = unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        dm = re.search(r"w dniu (\d{2})\.(\d{2})\.(\d{4})", title)
        rm = re.search(r"_([IVXLCDM]+)_sesji", url)
        if not dm or not rm:
            continue
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        if date < KAD_START:
            continue
        num = rm.group(1)
        sessions.append({"num": num, "num_i": _ROM.get(num, 0), "date": date, "title": title, "url": url, "aid": aid})
    # link PDF z artykułu
    for se in sessions:
        art = _get(se["url"], cache_dir).decode("utf-8", "ignore")
        pm = re.search(r'href="(https://bip\.miedzyrzecz\.pl/system/pobierz\.php[^"]+)"', art)
        se["pdf"] = pm.group(1).replace("&amp;", "&") if pm else None
    sessions.sort(key=lambda s: s["date"])
    return sessions


# ---------------- PDF parsing ----------------
def _col_tokens(words, table_min):
    """Zwróć listę tokenów głosu (dict: vote/text/x0/top) poniżej table_min."""
    rows = []
    for w in words:
        if w["top"] < table_min:
            continue
        txt = w["text"]
        if txt == "SIĘ" or txt == "się" or txt == "Się":
            continue
        if _is_vote_token(txt):
            rows.append({"vote": txt, "x0": w["x0"], "top": w["top"]})
    return rows


def _parse_pdf(data):
    """Zwraca listę głosów: [{topic, named:{za,przeciw,wstrzymal_sie,nieobecni}, meta_counts, matched}]."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        votes = []
        cur = None
        for page in pdf.pages:
            words = page.extract_words()
            text = page.extract_text() or ""
            # nowy głos zaczyna się na stronie z nagłówkiem agregatów ("kworum" albo "uprawnionych")
            is_new = ("Kworum" in text) or ("Liczba uprawnionych" in text)
            # znajdź znacznik "Uprawnieni do głosowania" (początek tabeli)
            up = [w for w in words if _nk(w["text"]) == "uprawnieni" and w["top"] > 200]
            table_min = (min(w["top"] for w in up) + 6) if up else 350.0

            if is_new:
                topic, agg = _header(page, words, text)
                rows = _col_tokens(words, table_min)
                cur = {"topic": topic, "agg": agg, "matched": []}
                votes.append(cur)
            else:
                if cur is None:
                    continue
                rows = _col_tokens(words, table_min)
            # zbierz nazwiska dla każdego tokenu głosu: obszar (kolumna Lp .. token głosu) w oknie pionowym
            for rt in rows:
                vote = rt["vote"]; vx = rt["x0"]; vt = rt["top"]
                # Lp wiersza = najbardziej wysunięty w prawo numer Lp na lewo od tokenu, w oknie pionowym
                lps = [w for w in words
                       if re.match(r"^\d+\.$", w["text"]) and w["x0"] < vx and abs(w["top"] - vt) <= 12]
                xlo = (max(lps, key=lambda w: w["x0"])["x0"] + 4) if lps else 0.0
                name_toks = []
                for w in words:
                    if w["top"] < table_min:
                        continue
                    if w["x0"] < xlo:
                        continue
                    if w["x0"] > vx - 4:
                        continue
                    if abs(w["top"] - vt) > 12:
                        continue
                    if re.match(r"^\d+\.$", w["text"]):
                        continue
                    if w["text"] in ("SIĘ", "się", "Się"):
                        continue
                    if _is_vote_token(w["text"]):
                        continue
                    if _nk(w["text"]) in ("lp", "nazwisko", "imie", "glos", "imię"):
                        continue
                    name_toks.append(w)
                name_toks.sort(key=lambda w: (w["top"], w["x0"]))
                name = " ".join(w["text"] for w in name_toks).strip()
                cur["matched"].append((name.strip(), vote))
        return votes


def _agg_from_lines(text):
    """Agregaty liczbowe typowe dla obu formatów. Zwraca dict lub None gdy brak."""
    agg = {}
    agg["uprawnionych"] = None
    # Głosy X N (te same linie w obu formatach)
    for key, pat in [("za", r"Głosy\s+za\s+(\d+)"),
                     ("przeciw", r"Głosy\s+przeciw\s+(\d+)"),
                     ("wstrzym", r"Głosy\s+wstrzymujące\s+się\s+(\d+)")]:
        m = re.search(pat, text)
        if m:
            agg[key] = int(m.group(1))
    # Liczba uprawnionych/obecnych/nieobecnych: liczba poprzedzająca "Głosy X"
    for key, pat in [("uprawnionych", r"(\d+)\s+Głosy\s+za"),
                     ("obecnych", r"(\d+)\s+Głosy\s+przeciw"),
                     ("nieobecnych", r"(\d+)\s+Głosy\s+wstrzymujące")]:
        m = re.search(pat, text)
        if m:
            agg[key] = int(m.group(1))
    # Obecni niegłosujący N (samo w linii w obu formatach)
    m = re.search(r"Obecni\s+niegłosujący\s+(\d+)", text)
    if m:
        agg["obecni_nieglosujacy"] = int(m.group(1))
    return agg


def _header(page, words, text):
    up = [w for w in words if _nk(w["text"]) == "uprawnieni" and w["top"] > 200]
    table_min = (min(w["top"] for w in up) + 4) if up else 1e9
    # agregaty
    agg = _agg_from_lines(text)
    # temat: między "Głosowanie" a "Typ" (x0>50)
    glos = [w for w in words if w["text"] == "Głosowanie"]
    typ = [w for w in words if w["text"] == "Typ"]
    topic = "(glosowanie)"
    if glos and typ and glos[0]["top"] < typ[0]["top"]:
        gtop = glos[0]["top"]; ttop = typ[0]["top"]
        toks = sorted((w for w in words
                       if w["top"] > gtop and w["top"] < ttop and w["x0"] > 50 and w["text"] != "Głosowanie"),
                      key=lambda w: (w["top"], w["x0"]))
        topic = re.sub(r"\s+", " ", " ".join(t["text"] for t in toks)).strip(" .:,;-")
    return (topic or "(glosowanie)"), agg


def _normalize_vote(txt):
    return _is_vote_token(txt)


def records_from_pdf(data):
    """Głosy -> list [{topic, named:{za,przeciw,wstrzymal_sie,nieobecni}}] po walidacji."""
    votes = _parse_pdf(data)
    out = []
    for v in votes:
        agg = v["agg"] or {}
        counter = Counter()
        bad = []
        for name, vote_txt in v["matched"]:
            norm = _normalize_vote(vote_txt)
            if norm is None:
                bad.append(vote_txt)
                continue
            counter[norm] += 1
        # walidacja z agregatami
        ok = (
            agg.get("za") is not None and
            counter.get("za", 0) == agg.get("za") and
            counter.get("przeciw", 0) == agg.get("przeciw", 0) and
            counter.get("wstrzymal_sie", 0) == agg.get("wstrzym", 0) and
            counter.get("nieobecni", 0) == agg.get("nieobecnych", 0) and
            counter.get("obecny", 0) == agg.get("obecni_nieglosujacy", 0)
        )
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for name, vote_txt in v["matched"]:
            norm = _normalize_vote(vote_txt)
            if norm in named:
                named[norm].append(name)
            # "obecny" (obecny niegłosujący) pomijany w listach imiennych, zgodnie z goleniow
        out.append({"topic": v["topic"], "named": named, "agg": agg or {}, "ok": ok, "bad": bad,
                    "n_matched": len(v["matched"])})
    return out


# ---------------- output (wzorcowe jak goleniow) ----------------
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
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
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
    total_votes = len(all_votes); total_sessions = len(sessions_data)
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
    from itertools import combinations
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, ""), "club_b": club_assign.get(b, ""),
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
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0, "votes_nieobecny": 0,
                "votes_total": total, "rebellion_count": 0, "rebellions": [],
                "roles": [], "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[miedzyrzecz] {len(sessions)} sesji IX kad. (I..{max(s['num'] for s in sessions)})")

    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    records = []
    vstat = {"v": 0, "ok": 0, "fail": 0}
    for se in sessions:
        pf = pdf_dir / f"{se['num']}.pdf"
        if not (pf.is_file() and pf.stat().st_size > 1000):
            if not args.skip_download and se.get("pdf"):
                data = _get(se["pdf"], cache)
                pf.write_bytes(data)
            else:
                print(f"  [skip {se['date']} no pdf cached]")
                continue
        try:
            recs = records_from_pdf(pf.read_bytes())
            nok = sum(1 for r in recs if r["ok"])
            vstat["v"] += len(recs); vstat["ok"] += nok; vstat["fail"] += len(recs) - nok
            for r in recs:
                r["date"] = se["date"]; r["num"] = se["num"]
            records += recs
            flag = "OK" if nok == len(recs) else f"VALID={nok}/{len(recs)}"
            print(f"  [ok {se['date']}] sesja {se['num']} votes={len(recs)} {flag}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[miedzyrzecz] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])} validated={vstat['ok']}/{vstat['v']} fail={vstat['fail']}")


if __name__ == "__main__":
    main()
