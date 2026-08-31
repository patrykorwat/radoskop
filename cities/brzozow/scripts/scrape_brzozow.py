#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Brzozów — imienne głosowania Rady Miejskiej w Brzozowie (DSSS Vote / bip.gov.pl).

Źródło: https://brzozow.bip.gov.pl (/protokoly-z-sesji-rady-miejskiej/), każdy protokół
ma załącznik 'glosowania_sesja_DD_MM_YYYY.pdf' = per-głosowanie załączniki DSSS Vote
z WARSTWĄ TEKSTOWĄ (name-list 'Jestem za / Jestem przeciw / Wstrzymuję się / BRAK').
Rekonstrukcja pozycyjna kolumn jak w szydlowiec (x<320 lewa, x>=320 prawa).
Roster: 21 radnych (poprzednia.brzozow.pl 'Skład i komisje') — prezydium: Dorota
Kamińska (przew.), Bogdan Duplaga + Grzegorz Pietryka (wice). 12 protokołów IX kad.

Użycie: python scrape_brzozow.py --city-dir cities/brzozow [--cache-dir .cache]
"""
import argparse, hashlib, json, re, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf
import requests

BASE = "https://brzozow.bip.gov.pl"
CAT = f"{BASE}/protokoly-z-sesji-rady-miejskiej/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024–2029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.6
_LAST = 0.0

ROLES = {
    "Dorota Kamińska": "Przewodnicząca Rady Miejskiej w Brzozowie",
    "Bogdan Duplaga": "Wiceprzewodniczący Rady Miejskiej w Brzozowie",
    "Grzegorz Pietryka": "Wiceprzewodniczący Rady Miejskiej w Brzozowie",
}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=True):
    if cache is not None:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        Path(cache, ).joinpath(hashlib.md5(url.encode()).hexdigest() + ".bin").write_bytes(resp.content)
    return resp.content


def _slugify(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


# ----------------------------------------------------------- sesje + załączniki
def discover_sessions(cache=None):
    """Protokoły z paginacją /articles/index/protokoly-z-sesji-rady-miejskiej/page:N."""
    prot = {}
    url = CAT
    for page_no in range(1, 15):
        u = url if page_no == 1 else f"{BASE}/articles/index/protokoly-z-sesji-rady-miejskiej/page:{page_no}"
        try:
            r = requests.get(u, headers=HEADERS, timeout=60)
        except Exception as e:
            print(f"  [ERR page {page_no}] {e}")
            break
        if r.status_code != 200:
            break
        found = 0
        for m in re.finditer(r'href="(/protokoly-z-sesji-rady-miejskiej/[^"]+\.html)"[^>]*>\s*Protokół Nr (\d+)/(\d{4})', r.text):
            h, n, y = m.group(1), int(m.group(2)), m.group(3)
            if h not in prot:
                prot[h] = {"url": BASE + h, "no": n, "year": y}
                found += 1
        if not found:
            break
        time.sleep(0.5)
    ix = [p for p in prot.values() if p["year"] >= "2024"]
    ix.sort(key=lambda p: (p["year"], p["no"]))
    return ix


def find_glosowania_pdf(prot_url, cache=None):
    r = _fetch_html(prot_url, cache)
    atts = re.findall(r'href="(/fobjects/download/(\d+)/[^"]+\.html)"[^>]*>\s*([^<]{0,120})', r)
    best = None
    for h, _id, t in atts:
        t = t.strip()
        if re.search(r"głosowania|glosowania|glosow", t, re.I) and "protokół" not in t.lower():
            best = BASE + h
            break
    if not best and atts:
        # fallback: largest single attachment
        best = BASE + [a[0] for a in atts if re.search(r"głosow|glosow", a[2], re.I)][0] \
            if any(re.search(r"głosow|glosow", a[2], re.I) for a in atts) else None
    return best


def _fetch_html(url, cache=None):
    if cache is not None:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        Path(cache, hashlib.md5(url.encode()).hexdigest() + ".html").write_text(
            resp.text, encoding="utf-8", errors="ignore")
    return resp.text


# ----------------------------------------------------------- parse (walidacja 10/10)
def _lines_in_column(words, x_lo, x_hi, y_lo, y_hi):
    sel = [w for w in words if x_lo <= w[0] < x_hi and y_lo <= w[1] < y_hi]
    sel.sort(key=lambda w: (round(w[1] / 6), w[0]))
    lines = defaultdict(list)
    for w in sel:
        lines[round(w[1] / 6)].append((w[0], w[4]))
    return [" ".join(t for _, t in sorted(lines[k], key=lambda z: z[0])) for k in sorted(lines)]


def _parse_list(lines):
    cat, cats = None, defaultdict(list)
    for ln in lines:
        low = ln.lower()
        if "jestem za" in low:
            cat = "za"
        elif "jestem przeciw" in low:
            cat = "przeciw"
        elif "wstrzymuj" in low and "się" in low:
            cat = "wstrzym"
        elif "obecni radni" in low or "nie wzięli" in low or low.startswith("udziału") or low.startswith("w głosowaniu"):
            cat = "obecni_no"
        elif cat and re.match(r"^\d+\.\s+[A-ZŁŚ]", ln):
            cats[cat].append(re.sub(r"^\d+\.\s+", "", ln).strip())
    return cats


def parse_doc(doc):
    votes = []
    for i in range(doc.page_count):
        pg = doc[i]
        words = pg.get_text("words")
        t = pg.get_text()
        za = re.search(r"jestem\s+za\s*[:]?\s*(\d+)", t, re.I)
        pr = re.search(r"jestem\s+przeciw\s*[:]?\s*(\d+)", t, re.I)
        wz = re.search(r"wstrzymuj\S*\s*się\s*[:]?\s*(\d+)", t, re.I)
        if not (za or pr):
            continue
        y_lo = 330
        for w in words:
            if w[4] == "zagłosowali":
                y_lo = w[1]
                break
        left = _lines_in_column(words, 0, 320, y_lo, 760)
        right = _lines_in_column(words, 320, 800, y_lo, 760)
        lc, rc = _parse_list(left), _parse_list(right)
        named = {"za": lc.get("za", []) + rc.get("za", []),
                 "przeciw": lc.get("przeciw", []) + rc.get("przeciw", []),
                 "wstrzymal_sie": lc.get("wstrzym", []) + rc.get("wstrzym", [])}
        counts = {k: len(v) for k, v in named.items()}
        agg = (int(za.group(1)), int(pr.group(1)), int(wz.group(1)) if wz else 0)
        got = (counts["za"], counts["przeciw"], counts["wstrzymal_sie"])
        if agg != got:
            continue  # nie reconciluje → pomiń (nie fabrykujemy)
        tm = re.search(r"w sprawie\s*[„“”\"]?\s*([^„“”\"\n]{5,160})", t)
        topic = tm.group(1).strip() if tm else ""
        dt = re.search(r"Data i godzina głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", t)
        vdate = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}" if dt else ""
        votes.append({"topic": topic, "named": named, "counts": counts, "session_date": vdate})
    return votes


# ----------------------------------------------------------- output (sample szydlowiec)
def build_output(records, session_map):
    sessions_by_date = {}
    all_votes = []
    vid = 0
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
                          "session_number": session_map.get(d, d), "topic": rec.get("topic", ""),
                          "named_votes": rec["named"], "counts": rec["counts"]})
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
                if cat == "za":
                    cc[nm]["za"] += 1
                elif cat == "przeciw":
                    cc[nm]["przeciw"] += 1
                else:
                    cc[nm]["wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    c_session = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c_session[nm].add(v["session_date"])
    councilors_list = []
    for nm in sorted(cc):
        present = sum(cc[nm].values())
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(c_session[nm]) / total_sessions * 100 if total_sessions else 0
        club = ""
        councilors_list.append({
            "name": nm, "club": club, "district": None,
            "role": ROLES.get(nm, ""),
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
        profiles.append({"name": nm, "slug": _slugify(nm),
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
    print(f"[brzozow] protokoły IX kad.: {len(sessions)}")
    records = []
    session_map = {}
    for s in sessions:
        href = find_glosowania_pdf(s["url"], cache)
        if not href:
            print(f"  [warn] brak pdf glosowania: protokol {s['no']}/{s['year']}")
            continue
        data = _fetch(href, cache, binary=True)
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            print(f"  [ERR pdf {s['no']}/{s['year']}] {e}")
            continue
        vs = parse_doc(doc)
        for v in vs:
            records.append({"topic": v["topic"], "named": v["named"],
                            "counts": v["counts"], "session_date": v["session_date"]})
        print(f"  prot.{s['no']}/{s['year']}: votes={len(vs)}")
    data, kad = build_output(records, session_map)
    profiles = build_profiles(records)
    save_split(data, kad, city_dir / "docs", profiles)
    print(f"[brzozow] total votes={kad['total_votes']} sessions={kad['total_sessions']} "
          f"councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
