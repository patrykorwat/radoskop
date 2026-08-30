#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Chełmek — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.malopolska.pl (platforma "Małopolska BIP" / Nowoczesna Gmina,
Angular SPA + backend API).
  * Kategoria "Protokoły z głosowań": /api/menu/309786/articles → per-sesja
    artykuły "N sesja IX kadencji Rady Miejskiej w Chełmku" (22 sesje IX kad.).
  * Artykuł: /api/articles/{id} → attachment "protokol (szczegółowy)" (PDF).
  * Pobranie PDF: /e,pobierz,get.html?id={attachmentId}.

Format PDF (tekstowy, eSesja per-głosowanie):
    Protokół głosowania
    z dnia DD-MM-YYYY
    <N> sesja IX kadencji Rady Miejskiej w Chełmku
    GŁOSOWANIE
    <numer>
    <temat uchwały>
    TYP GŁOSOWANIA / Jawne
    DATA GŁOSOWANIA / YYYY-MM-DD HH:MM:SS
    LICZBA UPRAWNIONYCH / N
    GŁOSY ZA / PRZECIW / WSTRZYMUJĄCE SIĘ / NIEODDANE / NIEOBECNYCH (agregaty)
    LICZBA OBECNYCH / N
    ...
    UPRAWNIENI DO GŁOSOWANIA
    LP / RADNY / GŁOS
    1 <Imię Nazwisko> <Za|Przeciw|Wstrzymuje się|Nieobecny>
    ...
  Każde głosowanie walidowane vs agregat (za+przeciw+wstrzymal == liczba list).

Użycie:
    python scrape_chelmek.py --city-dir cities/chelmek
"""
import argparse, hashlib, json, re, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://bip.malopolska.pl"
VOTES_CAT = "309786"          # "Protokoły z głosowań"
SESSION_HEADER_RE = re.compile(r'(\w+)\s+sesja\s+IX\s+kadencji')
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Radoskop/1.0",
           "Accept-Language": "pl,en"}
REQ_DELAY = 0.5
_LAST = 0.0

_GLOS = {
    'ZA': 'za', 'PRZECIW': 'przeciw',
    'WSTRZYMUJE SIĘ': 'wstrzymal_sie', 'WSTRZYMUJE SIE': 'wstrzymal_sie',
    'WSTRZYMAŁ SIĘ': 'wstrzymal_sie', 'WSTRZYMAL SIĘ': 'wstrzymal_sie',
    'WSTRZYMAŁO SIĘ': 'wstrzymal_sie', 'WSTRZYMALO SIE': 'wstrzymal_sie',
    'NIEOBECNY': 'nieobecny', 'NIEODDANY': 'nie_oddany',
}


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _get(url, cache=None, binary=False):
    key = hashlib.md5(("bin" if binary else "txt" + url).encode()).hexdigest()
    if cache is not None:
        cf = Path(cache) / (key + (".bin" if binary else ".json"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (key + (".bin" if binary else ".json"))).write_bytes(
            r.content if binary else r.content)
    return r.content if binary else r.content


def _get_json(url, cache=None):
    data = _get(url, cache)
    return json.loads(data.decode("utf-8", "replace")) if isinstance(data, bytes) else json.loads(data)


def discover_sessions(cache):
    """Artykuły 'Protokoły z głosowań' (309786), tylko IX kadencja."""
    out = []
    offset = 0
    while True:
        d = _get_json(f"{API}/api/menu/{VOTES_CAT}/articles?offset={offset}&limit=50", cache)
        arts = d.get("articles", [])
        total = d.get("total", 0)
        for a in arts:
            cf = {c["fieldId"]: c["value"] for c in a.get("columnFields", [])}
            title = cf.get("title", "")
            date = (cf.get("activeYMD") or "")[:10]
            # tylko IX kadencja
            if "IX kadencji" not in title:
                continue
            if date and date < KAD_START:
                continue
            out.append({"date": date, "title": title, "id": a["id"],
                        "link": a.get("link", "")})
        offset += 50
        if len(arts) < 50 or offset >= total:
            break
    return out


def _attachment_url(article_id, cache):
    d = _get_json(f"{API}/api/articles/{article_id}", cache)
    for at in d.get("attachments", []):
        name = (at.get("name") or "").lower()
        url = at.get("fileUrl") or at.get("url") or at.get("link") or ""
        if "szczegółowy" in name or "szczegolowy" in name:
            # e,pobierz,get.html?id=N
            m = re.search(r'get\.html\?id=(\d+)', url)
            if m:
                return f"{API}/e,pobierz,get.html?id={m.group(1)}"
            if url.startswith("http"):
                return url
    # fallback: pierwszy PDF attachment
    for at in d.get("attachments", []):
        url = at.get("fileUrl") or at.get("url") or at.get("link") or ""
        m = re.search(r'get\.html\?id=(\d+)', url)
        if m:
            return f"{API}/e,pobierz,get.html?id={m.group(1)}"
        if url.lower().endswith(".pdf") or ".pdf" in url.lower():
            return url
    return None


def read_cleaned_pdf(data):
    import fitz
    d = fitz.open(stream=data, filetype="pdf")
    pages = []
    for i in range(d.page_count):
        t = d[i].get_text()
        tl = t.split("\n")
        if tl and tl[0].strip().isdigit():
            tl = tl[1:]
        pages.append("\n".join(tl))
    return "\n".join(pages), d


def split_vote_blocks(text):
    """Podział na per-głosowanie bloki po markerze 'Protokół głosowania'."""
    parts = re.split(r'(?m)^\s*Protokół głosowania\s*$', text)
    blocks = []
    for p in parts[1:]:
        blocks.append(p)
    return blocks


def _topic_of(block):
    # Po 'GŁOSOWANIE' + numer: temat to pierwsze linie nie-etykietkowe
    lines = [l.strip() for l in block.split("\n")]
    after = False
    got_num = False
    for l in lines:
        if l == "GŁOSOWANIE":
            after = True
            continue
        if after and not got_num:
            if l.isdigit():
                got_num = True
            continue
        if after and got_num:
            if l and l.upper() not in ("TYP GŁOSOWANIA", "JAWNE") and "GŁOSOWANIA" not in l.upper():
                return l
    return ""


def _parse_rows(tail):
    lines = [l.strip() for l in tail.split("\n") if l.strip()]
    rows = []
    i = 0
    # skip header lines up to "LP/RADNY/GŁOS"
    while i < len(lines) and lines[i] != "GŁOS":
        i += 1
    i += 1  # past header
    while i < len(lines):
        if not lines[i].isdigit():
            i += 1
            continue
        # lp, then name (may span multiple), then vote token
        j = i + 1
        name_parts = []
        while j < len(lines) and lines[j].upper() not in _GLOS and not re.match(r'^\d+$', lines[j]):
            name_parts.append(lines[j]); j += 1
        if j < len(lines) and lines[j].upper() in _GLOS:
            nm = (" ".join(name_parts)).strip()
            if nm:
                rows.append((nm, _GLOS[lines[j].upper()]))
        i = j + 1
    return rows


def parse_pdf_text(full_text):
    out = []
    for raw in split_vote_blocks(full_text):
        dm = re.search(r'z dnia\s+(\d{2})-(\d{2})-(\d{4})', raw)
        date = None
        if dm:
            date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        topic = _topic_of(raw)
        agg = {}
        for key in ("LICZBA UPRAWNIONYCH", "GŁOSY ZA", "GŁOSY PRZECIW",
                    "GŁOSY WSTRZYMUJĄCE SIĘ", "GŁOSY NIEODDANE", "LICZBA NIEOBECNYCH"):
            m = re.search(r'(?m)^\s*' + re.escape(key) + r'\s*\n\s*(\d+)', raw)
            if m:
                agg[key] = int(m.group(1))
        # imienne listy po "UPRAWNIENI DO GŁOSOWANIA"
        gm = re.search(r'(?m)^\s*UPRAWNIENI DO GŁOSOWANIA\s*$', raw)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        if gm:
            for nm, gk in _parse_rows(raw[gm.end():]):
                if gk in named and nm:
                    named[gk].append(nm)
        za, przeciw, wz = agg.get("GŁOSY ZA", 0), agg.get("GŁOSY PRZECIW", 0), agg.get("GŁOSY WSTRZYMUJĄCE SIĘ", 0)
        if za != len(named["za"]) or przeciw != len(named["przeciw"]) or wz != len(named["wstrzymal_sie"]):
            continue
        out.append({"topic": topic, "date": date, "za": za, "przeciw": przeciw,
                    "wstrzymal_sie": wz, "named": named})
    return out


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": d, "vote_count": 0, "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        vid += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": d,
                          "topic": rec.get("topic", ""), "named_votes": rec["named"],
                          "counts": {"za": rec["za"], "przeciw": rec["przeciw"],
                                     "wstrzymal_sie": rec["wstrzymal_sie"]}})
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
    councilors = {n: {"name": n, "club": "", "district": None, "votes_za": 0,
                      "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
                      "votes_nieobecny": 0, "rebellions": []} for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            key = {"za": "votes_za", "przeciw": "votes_przeciw",
                   "wstrzymal_sie": "votes_wstrzymal"}.get(cat, "votes_wstrzymal")
            for nm in names:
                if nm in councilors:
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
    from itertools import combinations
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for x in common if vectors[a][x] == vectors[b][x])
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
    sess = set()
    n_votes = 0
    for rec in records:
        d = rec.get("date") or ""
        if d < KAD_START:
            continue
        n_votes += 1
        sess.add(d)
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    n_sessions = len(sess) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess_n = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, n_votes) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess_n / n_sessions * 100, 1),
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
    sessions = discover_sessions(cache)
    print(f"[chelmek] sessions (protokoły z głosowań, IX kad): {len(sessions)}")
    per_date = {}
    for s in sessions:
        url = _attachment_url(s["id"], cache)
        if not url:
            print(f"  [skip {s['date']}] brak załącznika")
            continue
        try:
            data = _get(url, cache, binary=True)
        except Exception as e:
            print(f"  [ERR fetch {s['date']}] {e}"); continue
        try:
            full_text, doc = read_cleaned_pdf(data)
        except Exception as e:
            print(f"  [ERR pdf {s['date']}] {e}"); continue
        vs = parse_pdf_text(full_text)
        if len(vs) == 0:
            print(f"  [stale/glitch {s['date']}] 0 zwalidowanych — pomijam")
            continue
        prev = per_date.get(s["date"])
        if prev is None or len(vs) > len(prev):
            per_date[s["date"]] = vs
        print(f"  {s['date']} votes={len(vs)}")
    records = []
    for sdate, vs in sorted(per_date.items()):
        for v in vs:
            v["date"] = sdate
            records.append(v)
    if not records:
        print("[chelmek] BRAK danych — nie generuję")
        return 1
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    cfg_path = city_dir / "config.json"
    if cfg_path.is_file():
        (city_dir / "docs" / "config.json").write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
    k = output["kadencje"][0]
    print(f"[chelmek] TOTAL votes={k['total_votes']} sessions={k['total_sessions']} councilors={k['total_councilors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
