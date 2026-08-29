#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Bystrzyca Kłodzka — imienne głosowania Rady Miejskiej (eSesja imienne TEXT w protokołach PDF).

Źródło: https://bip.bystrzycaklodzka.pl (idcom-jst BIP). Każda sesja publikuje
protokół PDF (tekstowy) z per-głosowanie blokami:
    Głosowano w sprawie:
    <temat>
    Wyniki głosowania
    ZA: N, PRZECIW: M, WSTRZYMUJĘ SIĘ: K, BRAK GŁOSU: B, NIEOBECNI: R
    Wyniki imienne:
    ZA (N)
    <nazwiska oddzielone przecinkami>
    PRZECIW (M)
    ...
    WSTRZYMUJĘ SIĘ (K)
    ...
Roster: 15 radnych (obecni z protokołu). Walidacja: nazwiska dopasowane do
rostera; każdy głos reconcilowany vs agregat (za+przeciw+wstrzymal == liczba
nazwisk). Głosy nienazwiste (jedyne) sapośledzane.
"""
import argparse, hashlib, json, re, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.bystrzycaklodzka.pl"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0"}
REQ_DELAY = 0.35
_LAST = 0.0

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

YEAR_PAGES = {
    2024: f"{BASE}/621/protokoly-z-sesji-rady-miejskiej-2024.html",
    2025: f"{BASE}/841/protokoly-z-sesji-rady-miejskiej-2025.html",
    2026: f"{BASE}/1067/protokoly-z-sesji-rady-miejskiej-2026.html",
}

def discover_sessions(cache):
    all_sess = {}
    for yr, url in YEAR_PAGES.items():
        html = _fetch(url, cache)
        # pdf attachment links: /download/attachment/{id}/{fname}.pdf
        for m in re.finditer(r'href="([^"]*download/attachment/(\d+)/([^"]+\.pdf)[^"]*)"', html):
            aid, fname = int(m.group(2)), m.group(3)
            if "protokol" not in fname.lower() and "proto" not in fname.lower():
                continue
            # date from filename: protokol-sesja-21082026.pdf  OR protokol-13052024.pdf
            dm = re.search(r'(?:sesja[-]?)?(\d{1,2})(\d{2})(\d{4})\.pdf$', fname)
            if not dm:
                continue
            d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            url = m.group(1)
            if url.startswith("/"):
                url = BASE + url
            all_sess[aid] = {"url": url, "date": iso, "fname": fname, "year": yr}
    # dedupe by date (a given session appears once per year page)
    by_date = {}
    for aid, s in all_sess.items():
        if s["date"] not in by_date or aid > by_date[s["date"]]["aid"]:
            s["aid"] = aid
            by_date[s["date"]] = s
    sess = [s for s in by_date.values() if s["date"] >= KAD_START]
    sess.sort(key=lambda s: s["date"])
    return sess

# ---------------------------------------------------------------------------
# Parser imiennych (eSesja TEXT w protokole)
# ---------------------------------------------------------------------------
_CAT_HDR = [
    ("ZA", "za"),
    ("PRZECIW", "przeciw"),
    ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
    ("WSTRZYMUJE SIĘ", "wstrzymal_sie"),
    ("BRAK GŁOSU", "brak_glosu"),
    ("BRAK GLOSU", "brak_glosu"),
    ("NIEOBECNI", "nieobecni"),
]

def parse_protocol(text, roster):
    """Zwraca listę głosowań: {topic, counts, named} — tylko reconciliowane."""
    votes = []
    # bloki głosowań
    parts = re.split(r"G[łl]osowano\s+w sprawie:\s*", text)
    for i in range(1, len(parts)):
        block = parts[i]
        # aggregate counts
        agg = re.search(r"ZA:\s*(\d+)\s*,\s*PRZECIW:\s*(\d+)\s*,\s*WSTRZYMUJ\S*\s+SIĘ:\s*(\d+)(?:\s*,\s*BRAK\s+GŁOSU:\s*(\d+))?(?:\s*,\s*NIEOBECNI:\s*(\d+))?",
                        block, re.I)
        if not agg:
            continue
        a, p, wz = int(agg.group(1)), int(agg.group(2)), int(agg.group(3))
        # topic = text between "Głosowano w sprawie:" and "Wyniki głosowania"
        tm = re.search(r"^(.*?)\s*\n\s*Wyniki\s+g[łl]osowania\s*\n", block, re.S)
        topic = tm.group(1).strip() if tm else ""
        topic = re.sub(r"\s+", " ", topic)
        # named section
        nm = re.search(r"Wyniki\s+imienne:\s*\n(.*)", block, re.S)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
        if nm:
            ntext = nm.group(1)
            named = parse_named(ntext, roster)
        za = [n for n in named.get("za", [])]
        pr = [n for n in named.get("przeciw", [])]
        wz_n = [n for n in named.get("wstrzymal_sie", [])]
        # reconciliation: aggregate za/przeciw/wstrzym must equal named lists
        if a != len(za) or p != len(pr) or wz != len(wz_n):
            # attempt re-parse with roster normalization may fix; else skip (don't fabricate)
            continue
        votes.append({"topic": topic, "counts": {"za": a, "przeciw": p, "wstrzymal_sie": wz},
                      "named": {"za": za, "przeciw": pr, "wstrzymal_sie": wz_n}})
    return votes

def parse_named(ntext, roster):
    """Parsuje sekcję 'Wyniki imienne'. Nazwiska oddzielone przecinkami, mogą być
    złamane wierszowo (linia kończy się w środku nazwiska). Kategorie nagłówkami
    'ZA (N)'. Każdy token-całość dopasowujemy do rosteru; nie rozcinamy na znaku
    nowej linii (to psuje 'Mariusz\\nLis'). Ogon narracyjny (np. 'Przewodniczący
    Rady stwierdził...') odcinamy przez prefiks-rostera."""
    result = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
    positions = []
    seen = set()
    for pattern, key in _CAT_HDR:
        for m in re.finditer(re.escape(pattern) + r"\s*\(\s*(\d+)\s*\)", ntext):
            if key in seen:
                continue
            if m.start() > len(ntext):
                continue
            positions.append((m.start(), m.end(), key))
            seen.add(key)
            break
    positions.sort(key=lambda x: x[0])
    for idx, (start, endd, key) in enumerate(positions):
        nstart = endd
        nend = positions[idx + 1][0] if idx + 1 < len(positions) else len(ntext)
        seg = ntext[nstart:nend]
        names = []
        # split ONLY on commas (names never contain commas); newline = wrap inside name
        for tok in seg.split(","):
            tok = re.sub(r"\s+", " ", tok).strip()
            if not tok:
                continue
            # odetnij ogon narracyjny: jeśli token zaczyna się od nazwiska z rosteru
            full = _exact_roster(tok, roster)
            if full:
                names.append(full)
                continue
            # token to prefiks nazwiska (ucięty ogon narracyjny na końcu listy)
            pre = _prefix_roster(tok, roster)
            if pre:
                names.append(pre)
        # dedupe (osoba głosuje raz)
        seen_n = set()
        uniq = []
        for n in names:
            if n not in seen_n:
                seen_n.add(n)
                uniq.append(n)
        result[key] = uniq
    return result


def _exact_roster(tok, roster):
    tn = _norm(tok)
    if not tn:
        return None
    for r in roster:
        if _norm(r) == tn:
            return r
    return None


def _prefix_roster(tok, roster):
    """True if token STARTS with a roster full-name (trailing narrative)."""
    tn = _norm(tok)
    if not tn:
        return None
    best, bestlen = None, -1
    for r in roster:
        rn = _norm(r)
        if tn.startswith(rn) and len(rn) >= 6 and len(rn) > bestlen:
            best, bestlen = r, len(rn)
    return best

def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", s.lower()).strip()

def _roster_match(tok, roster):
    """Najlepsze dopasowanie tokenu do rosteru (prefix rosnący)."""
    if not roster:
        return tok
    tn = _norm(tok)
    if not tn:
        return None
    best, bestlen = None, -1
    for r in roster:
        rn = _norm(r)
        # exact full name
        if tn == rn:
            return r
        # token is prefix of roster name (zawiera tylko imię/nazwisko -> linia złamana)
        if rn.startswith(tn) and len(tn) >= 4 and len(tn) > bestlen:
            best, bestlen = r, len(tn)
        if tn.startswith(rn) and len(rn) >= 4 and len(rn) > bestlen:
            best, bestlen = r, len(rn)
    return best

def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def build_output(records, session_map, session_attendance):
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
        sessions_by_date[d]["vote_count"] += 1
        vid += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": session_map.get(d, d),
            "topic": rec.get("topic", ""), "named_votes": rec["named"],
            "counts": rec["counts"],
        })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        at = session_attendance.get(d)
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(at) if at else len(s["attendees"]),
            "attendees": sorted(at) if at else sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {n: {"name": n, "club": "", "district": None,
                      "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                      "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []} for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors:
                    councilors[nm][{"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal"}.get(cat, "votes_wstrzymal")] += 1
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
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    from itertools import combinations
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
        same = sum(1 for vidx in common if vectors[a][vidx] == vectors[b][vidx])
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
    for rec in records:
        d = rec.get("session_date") or ""
        if d < KAD_START:
            continue
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
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100
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
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def _attendance_from_protocol(text):
    """Obecni radni z listy 'Obecni radni:' w protokole (plus Nieobecni jako uzupełnienie rosteru)."""
    names = []
    # Obecni
    m = re.search(r"Obecni radni:\s*\n(.*?)(?:\n\s*\n|\nPorządek|\nAd\.)", text, re.S)
    if m:
        for ln in m.group(1).split("\n"):
            ln = ln.strip()
            mm = re.match(r"^\d+\.\s+(.+)$", ln)
            if mm:
                names.append(re.sub(r"\s+", " ", mm.group(1)).strip())
    # Nieobecni (też radni — choć nie głosują tu, budują kompletny roster)
    m2 = re.search(r"Nieobecni radni:\s*\n(.*?)(?:\n\s*\n|\nAd\.|\nPorządek)", text, re.S)
    if m2:
        for ln in m2.group(1).split("\n"):
            ln = ln.strip()
            mm = re.match(r"^\d+\.\s+(.+)$", ln)
            if mm:
                nm = re.sub(r"\s+", " ", mm.group(1)).strip()
                if nm not in names:
                    names.append(nm)
    # Odrzuć śmieci (wpisy porządku dziennego) — nazwisko to 2-4 słowa, bez kropki
    names = [n for n in names
             if not n.endswith(".") and 2 <= len(n.split()) <= 4
             and not n.lower().startswith(("otwarcie", "stwierdzenie", "wskazanie",
                                            "podjęcie", "interpelacje", "zamknięcie",
                                            "przyjęcie", "rozpatrzenie", "wybór"))]
    return names

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[bystrzyca-klodzka] sesje (IX kad, >= {KAD_START}): {len(sessions)}")
    # Pass 1: pobierz protokoły, wyciągnij tekst + obecności; zbuduj globalny roster
    docs = {}
    session_attendance = {}
    global_roster = []
    import fitz
    for s in sessions:
        data = _fetch(s["url"], cache, binary=True)
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            text = "".join(p.get_text() for p in doc)
        except Exception as e:
            print(f"  [ERR pdf {s['date']}] {e}")
            continue
        if not text.strip():
            print(f"  [SKIP {s['date']}] pusta warstwa tekstowa (scan?)")
            continue
        docs[s["date"]] = {"text": text, "fname": s["fname"]}
        attendance = _attendance_from_protocol(text)
        session_attendance[s["date"]] = attendance
        for nm in attendance:
            if nm not in global_roster:
                global_roster.append(nm)
    print(f"  global roster ({len(global_roster)}): {global_roster}")
    # Pass 2: parsuj głosowania z globalnym rosterem
    records = []
    session_map = {}
    for d, dd in docs.items():
        vs = parse_protocol(dd["text"], global_roster)
        session_map[d] = dd["fname"][:40]
        for v in vs:
            v["session_date"] = d
            records.append(v)
        print(f"  {d} votes={len(vs)} obecni={len(session_attendance.get(d, []))}")
    output = build_output(records, session_map, session_attendance)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[bystrzyca-klodzka] TOTAL votes={k['total_votes']} sessions={k['total_sessions']} councilors={k['total_councilors']}")

if __name__ == "__main__":
    main()
