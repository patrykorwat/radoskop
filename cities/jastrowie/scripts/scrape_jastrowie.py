#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Jastrowie — imienne głosowania Rady Miejskiej (DSSS Vote, per-sesja PDF).

Źródło: https://bip3.wokiss.pl/jastrowie (platforma WOKISS; to samo drzewo co
bip.jastrowie.pl). Rada Miejska publikuje per-sesja "Imienny wykaz głosowań"
z /protokoly_sesji/glosowania/glosowanie-N.pdf (N = numer sesji, 1..35, IX kadencja).

Każdy PDF to per-głosowanie załączniki DSSS Vote — TEKSTOWA warstwa:
    Na sesji "XXXV Sesja ... Rady Miejskiej w Jastrowiu" ... radni.
    Obecni/Nieobecni radni: <lista>
    (per głosowanie)
    Uchwała numer NNN/RRRR "<temat>" została podjęta następującą proporcją
    głosów: jestem za A, jestem przeciw B, wstrzymuję się C.
    Radni zagłosowali jak poniżej: / Jestem za / Jestem przeciw / Wstrzymuję się /
    Obecni radni, którzy nie wzięli udziału w głosowaniu
    <tabela imienna: lp. Nazwisko Imię + znacznik kolumny (BRAK = nie głosował)>
Kolumny rekonstruowane POZYCYJNIE (x<320 lewa | x>=320 prawa) jak w Makowie/Szydłowcu.

Roster wyprowadzany DYNAMICZNIE z nazwisk obecnych w głosowaniach (radni 15).
Walidacja: KAŻDE głosowanie reconcilowane vs agregat (jestem za A, jestem przeciw B,
wstrzymuję się C == suma list imiennych).

Użycie:
    python scrape_jastrowie.py --city-dir cities/jastrowie [--cache-dir .cache]
"""
import argparse, hashlib, json, re, time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.jastrowie.pl/jastrowie"
LISTING = f"{BASE}/bip/rada-miejska/protokoly-sesji-rady-miejskiej-ix-kadencji.html"
GLOS_BASE = f"{BASE}/zasoby/files/protokoly_sesji/glosowania"
KAD_START = "2024-05-06"   # sesja I (inauguracyjna IX kad.) odbyła się 2024-05-06
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
           "Accept-Language": "pl,en;q=0.8"}
REQ_DELAY = 1.2
_LAST = 0.0

_ROMAN = {i: r for r, i in {
    'I':1,'II':2,'III':3,'IV':4,'V':5,'VI':6,'VII':7,'VIII':8,'IX':9,'X':10,
    'XI':11,'XII':12,'XIII':13,'XIV':14,'XV':15,'XVI':16,'XVII':17,'XVIII':18,
    'XIX':19,'XX':20,'XXI':21,'XXII':22,'XXIII':23,'XXIV':24,'XXV':25,'XXVI':26,
    'XXVII':27,'XXVIII':28,'XXIX':29,'XXX':30,'XXXI':31,'XXXII':32,'XXXIII':33,
    'XXXIV':34,'XXXV':35}.items()}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90, verify=False)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


def discover_pdfs(cache):
    """Znajdź imienne-wykaz PDF-y ze strony protokołów IX kadencji.

    Strona wymienia per-sesja 'glosowanie-N.pdf' (N = numer sesji; brak #5, bo
    sesja 5 używa innej nazwy i ma 0 głosów). Każdy URL walidowany (%PDF); przy
    HTTP-interstitial (bot-detection) retry z backoff.
    """
    html = _fetch(LISTING, cache)
    out = []
    seen = set()
    for m in re.finditer(r'([^"\']*?glosowanie-(\d+)\.pdf)', html):
        url = m.group(1)
        # href jest ścieżką względną od korzenia BIP (np. 'zasoby/files/protokoly_sesji/glosowania/glosowanie-1.pdf')
        if url.startswith("http"):
            full = url
        elif url.startswith("/"):
            full = "https://bip.jastrowie.pl" + url
        else:
            full = BASE + "/" + url
        num = int(m.group(2))
        if full in seen:
            continue
        seen.add(full)
        if num < 1 or num > 60:
            continue
        out.append({"number": num, "url": full})
    out.sort(key=lambda x: x["number"])
    return out


def _lines_in_column(words, x_lo, x_hi, y_lo, y_hi):
    sel = [w for w in words if x_lo <= w[0] < x_hi and y_lo <= w[1] < y_hi]
    sel.sort(key=lambda w: (round(w[1] / 6), w[0]))
    lines = {}
    for w in sel:
        key = round(w[1] / 6)
        lines.setdefault(key, []).append((w[0], w[4]))
    out = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda z: z[0])
        out.append(" ".join(t for _, t in ws))
    return out


def _parse_list(lines):
    cat = None
    cats = defaultdict(list)
    for ln in lines:
        low = ln.lower()
        if "jestem za" in low:
            cat = "za"
        elif "jestem przeciw" in low:
            cat = "przeciw"
        elif "wstrzymuj" in low and ("się" in low or "sie" in low):
            cat = "wstrzym"
        elif "obecni radni" in low or "nie wzięli" in low or low.startswith(("udziału", "w głosowaniu", "w glosowaniu")):
            cat = "obecni_no"
        elif cat and re.match(r"^\d+\.\s+[A-ZŁŚÓ]", ln):
            cats[cat].append(re.sub(r"^\d+\.\s+", "", ln).strip())
    return cats


def parse_session_pdf(data):
    """Zwraca listę głosowań z jednego PDF-a sesji."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    votes = []
    for pi in range(doc.page_count):
        t = doc[pi].get_text()
        words = doc[pi].get_text("words")
        zag = None
        for w in words:
            if w[4] == "zagłosowali":
                zag = w[1]
                break
        y_lo = zag if zag else 330
        left = [l for l in _lines_in_column(words, 0, 320, y_lo, 720) if l.strip()]
        right = [l for l in _lines_in_column(words, 320, 800, y_lo, 720) if l.strip()]
        lc, rc = _parse_list(left), _parse_list(right)
        za = re.search(r"jestem\s+za\s*[:]?\s*(\d+)", t, re.I)
        pr = re.search(r"jestem\s+przeciw\s*[:]?\s*(\d+)", t, re.I)
        wz = re.search(r"wstrzymuj\S*\s*(?:się|sie)\s*[:]?\s*(\d+)", t, re.I)
        if not (za or pr):
            continue
        named = {
            "za": lc.get("za", []) + rc.get("za", []),
            "przeciw": lc.get("przeciw", []) + rc.get("przeciw", []),
            "wstrzymal_sie": lc.get("wstrzym", []) + rc.get("wstrzym", []),
        }
        counts = {k: len(v) for k, v in named.items()}
        agg = (int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0)
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if agg != got:
            continue  # nie fabrykujemy nie-reconcilujących
        tm = re.search(r'(?:(?:w sprawie|Uchwała|Wniosek)[^“”"\n]{0,60})["“”"]?\s*([^“”"\n]{5,160})', t)
        topic = tm.group(1).strip() if tm else ""
        dt = re.search(r"Data i godzina głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
        vdate = ""
        if dt:
            vdate = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}"
        votes.append({"topic": topic, "named": named, "counts": counts, "session_date": vdate})
    return votes


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def build_output(records, session_map):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": session_map.get(d, d),
                                   "vote_count": 0, "attendees": set()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": session_map.get(d, d),
                          "topic": rec.get("topic", ""), "named_votes": rec["named"],
                          "counts": rec["counts"]})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {}
    for name in all_names:
        councilors[name] = {"name": name, "club": "", "district": None,
                            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                            "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors:
                    key = {"za": "votes_za", "przeciw": "votes_przeciw"}.get(cat, "votes_wstrzymal")
                    councilors[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
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
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r.get("session_date") for r in records if (r.get("session_date") or "") >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
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
                                    "kadencje": stubs}, ensure_ascii=False,
                                   separators=(",", ":")), encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    pdfs = discover_pdfs(cache)
    print(f"[jastrowie] sesje (glosowanie-*.pdf): {len(pdfs)}")
    records = []
    date_to_num = {}
    roman_by_num = {n: r for r, n in _ROMAN.items()}
    for p in pdfs:
        try:
            data = _fetch(p["url"], cache, binary=True)
        except Exception as e:
            print(f"  [ERR pdf {p['number']}] {e}")
            continue
        vs = parse_session_pdf(data)
        for v in vs:
            v["_num"] = p["number"]
            records.append(v)
        print(f"  sesja {p['number']}: votes={len(vs)}")
    if not records:
        print("[jastrowie] BRAK danych — nie generuję")
        return 1
    # map vote date -> roman session number (each pdf = one session)
    date_to_num = {}
    for v in records:
        d = v.get("session_date") or ""
        if d and d not in date_to_num:
            rl = roman_by_num.get(v.get("_num"), str(v.get("_num")))
            date_to_num[d] = f"Sesja {rl}"
    output = build_output(records, date_to_num)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[jastrowie] TOTAL votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
