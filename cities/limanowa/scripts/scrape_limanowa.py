#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Limanowa — imienne głosowania Rady Miasta Limanowa (IX kadencja 2024-2029).

Źródło: BIP bip.malopolska.pl/umlimanowa (platforma Małopolska BIP / Madkom,
Angular SPA + backend API; miasto NIE ma eSesja/Nefeni — limanowa.esesja.pl = wildcard).
  * Kategoria 'Rada Miasta Limanowa → Imienny wykaz głosowania' (menu id 312044):
    /api/menu/312044/articles → per-sesja artykuły 'NNN sesja Rady Miasta Limanowa
    w dniu D miesiąca YYYY roku' (daty tytułu; filtr >= 2024-05-07).
  * Artykuł: /api/articles/{id} → attachments[0] 'protokol_<data>' (PDF tekstowy).
  * Pobranie PDF: /e,pobierz,get.html?id={attachmentId}.

Format PDF (eSesja print, per-głosowanie blok 'Protokół głosowania'):
    GŁOSOWANIE / <numer> / <temat>
    TYP GŁOSOWANIA Jawne / DATA GŁOSOWANIA YYYY-MM-DD HH:MM:SS
    agregaty rozstrzelone: 'ZA \\n n ... PRZECIW \\n n ... WSTRZYMUJĘ SIĘ \\n n'
    UPRAWNIENI DO GŁOSOWANIA / LP RADNY GŁOS / po jednym wierszu:
    <lp> <Imię Nazwisko> <ZA|PRZECIW|WSTRZYMUJĘ SIĘ|NIEOBECNY>
  Każde głosowanie walidowane vs agregat (nie reconciluje → pomiń).

Roster: 15 radnych (unia nazwisk z list imiennych, 'UPRAWNIENI DO GŁOSOWANIA').
Przewodniczący: za BIP 'Skład Rady' — roles uzupełniane ręcznie jeśli weryfikowalne.

Użycie: python scrape_limanowa.py --city-dir cities/limanowa [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse, hashlib, json, re, time, unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://bip.malopolska.pl"
UNIT = "umlimanowa"
VOTES_MENU = "312044"   # 'Imienny wykaz głosowania'
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Radoskop/1.0",
           "Accept-Language": "pl,en"}
REQ_DELAY = 0.4
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}

_GLOS = {"ZA": "za", "PRZECIW": "przeciw",
         "WSTRZYMUJĘ SIĘ": "wstrzymal_sie", "WSTRZYMUJE SIĘ": "wstrzymal_sie",
         "WSTRZYMAŁ SIĘ": "wstrzymal_sie", "WSTRZYMAL SIĘ": "wstrzymal_sie",
         "NIEOBECNY": "nieobecny", "NIEODDANY": "nie_oddany"}
# wariant starszych protokołów: 'Za/Przeciw/Wstrzymuje się/Wstrzymał się/Nieobecny'
_GLOS.update({k.upper(): v for k, v in {
    "Za": "za", "Przeciw": "przeciw", "Wstrzymuje się": "wstrzymal_sie",
    "Wstrzymał się": "wstrzymal_sie", "Nieobecny": "nieobecny"}.items()})


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _get(url, cache=None, binary=False):
    key = hashlib.md5((("b" if binary else "t") + url).encode()).hexdigest()
    if cache is not None:
        cf = Path(cache) / (key + (".bin" if binary else ".dat"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    r = requests.get(url, headers=HEADERS, timeout=90, verify=False)
    r.raise_for_status()
    out = r.content if binary else r.text.encode("utf-8", "ignore")
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (key + (".bin" if binary else ".dat"))).write_bytes(out)
    return r.content if binary else r.text


def _get_json(url, cache=None):
    return json.loads(_get(url, cache))


def _title_of(a):
    return next((f["value"] for f in a.get("columnFields", []) if f["fieldId"] == "title"), "")


def discover_sessions(cache=None):
    """Artykuły 'Imienny wykaz głosowania' z datą w tytule >= KAD_START."""
    arts, off = [], 0
    while True:
        d = _get_json(f"{API}/api/menu/{VOTES_MENU}/articles?offset={off}&limit=50", cache)
        batch = d.get("articles") or []
        arts += batch
        if len(arts) >= (d.get("total") or 0) or not batch:
            break
        off += 50
    out = []
    for a in arts:
        t = _title_of(a)
        m = re.search(r"w dniu (\d{1,2}) (\w+) (\d{4})", t)
        if not m:
            continue
        mon = MONTHS.get(m.group(2).lower())
        if not mon:
            continue
        date = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
        if date < KAD_START:
            continue
        num = re.match(r"([IVXLCDM]+)\s+sesja", t)
        out.append({"article_id": str(a["id"]), "date": date,
                    "number": num.group(1) if num else date, "title": t})
    out.sort(key=lambda s: s["date"])
    return out


def attachment_pdfs(article_id, cache=None):
    """Wszystkie PDF-y artykułu (nowszy wariant: 1 'protokol_*'; starszy: 'Punkt N.pdf')."""
    d = _get_json(f"{API}/api/articles/{article_id}", cache)
    urls = []
    for att in d.get("attachments") or []:
        if (att.get("extension") or "").lower() == "pdf" and att.get("downloadable"):
            urls.append(f"{API}/e,pobierz,get.html?id={att['id']}")
    return urls


def read_pdf_text(data):
    import fitz
    d = fitz.open(stream=data, filetype="pdf")
    pages = []
    for i in range(d.page_count):
        tl = d[i].get_text().split("\n")
        while tl and (re.match(r"^\s*\d+-\d+-\d+\s*$", tl[0]) or re.match(r"^\s*\d+\s+z\s+\d+\s*$", tl[0])):
            tl = tl[1:]
        pages.append("\n".join(tl))
    return "\n".join(pages)


def _agg_of(block):
    a = {}
    # wariant nowszy: rozstrzelone 'ZA\n n'; wariant starszy: 'GŁOSY ZA\n n'
    m = re.search(r"(?m)^GŁOSY ZA\s*\n\s*(\d+)", block) or re.search(r"(?m)^ZA\s*\n\s*(\d+)", block)
    a["za"] = int(m.group(1)) if m else None
    m = re.search(r"(?m)^GŁOSY PRZECIW\s*\n\s*(\d+)", block) or re.search(r"(?m)^PRZECIW\s*\n\s*(\d+)", block)
    a["przeciw"] = int(m.group(1)) if m else None
    m = re.search(r"(?m)^GŁOSY WSTRZYMUJ(?:Ą|A)CE SIĘ\s*\n\s*(\d+)", block) or \
        re.search(r"(?m)^WSTRZYMUJĘ SIĘ\s*\n\s*(\d+)", block)
    a["wz"] = int(m.group(1)) if m else 0
    m = re.search(r"(?m)^DATA\s*\nGŁOSOWANIA\s*\n(\d{4}-\d{2}-\d{2})", block) or \
        re.search(r"(?m)^DATA GŁOSOWANIA\s*\n(\d{4}-\d{2}-\d{2})", block)
    a["date"] = m.group(1) if m else ""
    return a


def _topic_of(block):
    lines = [l.strip() for l in block.split("\n")]
    try:
        i = lines.index("GŁOSOWANIE")
    except ValueError:
        return ""
    # po numerze głosowania: linie do 'TYP GŁOSOWANIA'
    i += 2
    got = []
    for l in lines[i:]:
        if not l:
            continue
        if l.upper().startswith("TYP") or "GŁOSOWANIA" in l.upper():
            break
        got.append(l)
    return " ".join(got).strip()[:200]


def _rows_of(block):
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    try:
        i = lines.index("GŁOS")
    except ValueError:
        return []
    rows, i = [], i + 1
    while i < len(lines):
        if not re.match(r"^\d+$", lines[i]):
            i += 1
            continue
        j, name = i + 1, []
        while j < len(lines) and lines[j].upper() not in _GLOS and not re.match(r"^\d+$", lines[j]):
            name.append(lines[j]); j += 1
        if j < len(lines) and lines[j].upper() in _GLOS and name:
            rows.append((" ".join(name).strip(), _GLOS[lines[j].upper()]))
        i = j + 1
    return rows


def parse_pdf_text(full_text):
    out = []
    parts = re.split(r"(?m)^\s*Protokół głosowania\s*$", full_text)
    for block in parts[1:]:
        a = _agg_of(block)
        if a["za"] is None:
            continue
        gm = re.search(r"(?m)^\s*UPRAWNIENI DO GŁOSOWANIA\s*$", block)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        for nm, gk in _rows_of(block[gm.end():] if gm else block):
            if gk in named and nm:
                named[gk].append(nm)
        if (len(named["za"]), len(named["przeciw"]), len(named["wstrzymal_sie"])) != (a["za"], a["przeciw"], a["wz"]):
            continue
        out.append({"topic": _topic_of(block), "date": a["date"],
                    "za": a["za"], "przeciw": a["przeciw"], "wstrzymal_sie": a["wz"], "named": named})
    return out


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = unicodedata.normalize("NFKD", name.lower().replace("ł", "l"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def build_output(records, session_map):
    sessions_by_date, all_votes, vid = {}, [], 0
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
                          "topic": rec.get("topic", ""), "title": rec.get("topic", ""),
                          "named_votes": {k: list(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
                          "counts": {"za": rec["za"], "przeciw": rec["przeciw"], "wstrzymal_sie": rec["wstrzymal_sie"]},
                          "source_url": rec.get("source_url", "")})
    sessions_data = [{"date": d, "number": sessions_by_date[d]["number"],
                      "vote_count": sessions_by_date[d]["vote_count"],
                      "attendee_count": len(sessions_by_date[d]["attendees"]),
                      "attendees": sorted(sessions_by_date[d]["attendees"]), "speakers": []}
                     for d in sorted(sessions_by_date)]
    cc = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                cc[nm]["wstrzymal" if cat == "wstrzymal_sie" else cat] += 1
    total_votes, total_sessions = len(all_votes), len(sessions_data)
    c_session = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                c_session[nm].add(v["session_date"])
    councilors_list = []
    for nm in sorted(cc):
        present = sum(cc[nm].values())
        councilors_list.append({
            "name": nm, "club": "", "district": None, "role": "",
            "frekwencja": round(len(c_session[nm]) / total_sessions * 100 if total_sessions else 0, 1),
            "aktywnosc": round(present / total_votes * 100 if total_votes else 0, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc[nm]["za"], "votes_przeciw": cc[nm]["przeciw"],
            "votes_wstrzymal": cc[nm]["wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
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
        same = sum(1 for k in common if vectors[a][k] == vectors[b][k])
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
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                sess[nm].add(d)
    n_sessions = len({r.get("session_date") for r in records if r.get("session_date", "") >= KAD_START}) or 1
    total_records = sum(1 for r in records if r.get("session_date", "") >= KAD_START)
    profiles = []
    for nm in sorted(cv):
        vd = cv[nm]
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, total_records) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": "", "role": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(len(sess[nm]) / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
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
    print(f"[limanowa] sesje IX kad. z 'Imienny wykaz głosowania': {len(sessions)}")
    records, session_map = [], {}
    for s in sessions:
        session_map[s["date"]] = s["number"]
        urls = attachment_pdfs(s["article_id"], cache)
        if not urls:
            print(f"  [warn] brak PDF: {s['title'][:60]}")
            continue
        votes = []
        for url in urls:
            try:
                data = _get(url, cache, binary=True)
                votes += parse_pdf_text(read_pdf_text(data))
            except Exception as e:
                print(f"  [ERR {s['date']} att] {e}")
                continue
        for v in votes:
            records.append({"topic": v["topic"], "named": v["named"],
                            "za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"],
                            "session_date": v["date"] or s["date"],
                            "source_url": f"https://bip.malopolska.pl/{UNIT},a,{s['article_id']},artykul.html"})
        print(f"  {s['date']} sesja {s['number']}: votes={len(votes)}")
    data, kad = build_output(records, session_map)
    profiles = build_profiles(records)
    save_split(data, kad, city_dir / "docs", profiles)
    print(f"[limanowa] total votes={kad['total_votes']} sessions={kad['total_sessions']} "
          f"councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
