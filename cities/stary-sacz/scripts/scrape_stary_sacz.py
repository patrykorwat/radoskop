#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Stary Sącz — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.malopolska.pl / starysacz (platforma Wrota Małopolski / Madkom SPA).
Rada Miejska -> IMIENNE WYKAZY GŁOSOWAŃ RADNYCH (menu 319490) -> 
  IX KADENCJA RADY MIEJSKIEJ (menu 438659) — 29 sesji. Każdy artykuł = sesja
  ("XXXV Sesja Rady Miejskiej w Starym Sączu - 27 lipca 2026 r.") z załącznikiem
  PDF 'glosowanieSesja<N>' (format eSesja PRINT, tekstowy):
    <N> <XXXV Sesja Rady Miejskiej w Starym Sączu>
    Głosowanie
    <pkt>. <temat>.
    Typ głosowania jawne  Data głosowania: DD.MM.YYYY HH:MM
    Liczba uprawnionych 21  Głosy za 18
    Liczba obecnych 18      Głosy przeciw 0
    Liczba nieobecnych 3    Głosy wstrzymujące się 0
    Obecni niegłosujący 0
    Uprawnieni do głosowania
    Lp Nazwisko i imię Głos    Lp Nazwisko i imię Głos
    1. Bawełkiewicz Janina ZA  12. Ptak Dariusz NIEOBECNY
    ...
Każde głosowanie walidowane vs agregaty (za+przeciw+wstrzym+niegłosujący+nieobecni).
Nazwy 'Nazwisko Imię' odwracane; kanonizacja fuzzy wariantów OCR do realnych radnych.

Użycie: python scrape_stary_sacz.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""
import argparse, hashlib, io, json, os, re, sys, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
import requests, urllib3, pdfplumber
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.malopolska.pl"
API = BASE + "/api"
ENTITY = "starysacz"
GLOS_MENU = 438659          # IMIENNE WYKAZY GŁOSOWAŃ RADNYCH / IX KADENCJA RADY MIEJSKIEJ
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0"}
REQ_DELAY = 0.4
_LAST = 0.0

_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'pazdziernika': 10, 'wrzesnia': 9}

_VOTE_TOKENS = (r"ZA|PRZECIW|WSTRZYMUJ[AĄ]CY\s+SIĘ|WSTRZYMUJ[AĄ]CA\s+SIĘ|WSTRZYM[AŁY]\s+SIĘ|"
                r"WSTRZYM[AŁA]\s+SIĘ|WSTRZYM[AŁL]\s+SIĘ|NIEOBECN\w*|OBECN\w*|"
                r"NIE\s+GŁOSUJ[AĄ]CY|NIE\s+GŁOSUJ[AĄ]CA|NIE\s+G[ŁL]OSOWA[ŁL]|NIEODDAN\w*|"
                r"BEZ\s+G[ŁL]OSU|PRZECIWNY|WSTRZYMUJ\w*\.?\s+SIĘ\b")
_PAIR_RE = re.compile(
    r"(?:(?:[0-9]{1,3}|[A-Za-z]{1,2})\.?\s+)?([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻŻa-ząćęłńóśźż.\- ]*?)\s+(" +
    _VOTE_TOKENS + r")\b", re.I)


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def gj(url, cache=None):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + ".json")
        if cf.is_file():
            return json.loads(cf.read_text(encoding="utf-8"))
    _rate()
    r = requests.get(url, headers=UA, timeout=40, verify=False)
    r.raise_for_status()
    d = r.json()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ".json")).write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def gb(url, cache, key):
    Path(cache).mkdir(parents=True, exist_ok=True)
    p = Path(cache) / f"{key}.pdf"
    if p.is_file() and p.stat().st_size > 0:
        return p.read_bytes()
    _rate()
    r = requests.get(url, headers=UA, timeout=60, verify=False)
    r.raise_for_status()
    p.write_bytes(r.content)
    return r.content


def session_date_from_title(title):
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", title)
    if m and m.group(2).lower() in _MON:
        return f"{m.group(3)}-{_MON[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return ""


def session_roman_from_title(title):
    m = re.search(r"\b([IVXLCDM]+)\s*[Ss]esja", title)
    return m.group(1).upper() if m else ""


def collect_sessions(cache):
    d = gj(f"{API}/menu/{GLOS_MENU}/articles?limit=200", cache)
    arts = d.get("articles") or []
    sessions = {}
    for a in arts:
        aid = a.get("id")
        det = gj(f"{API}/articles/{aid}", cache)
        title = det.get("title") or ""
        date = session_date_from_title(title)
        roman = session_roman_from_title(title)
        if not date or date < KAD_START:
            # stawiaj sesje spoza IX kadencji na bok
            if date:
                continue
        atts = [x for x in (det.get("attachments") or [])
                if (x.get("extension") or "").lower() == "pdf" and
                re.search(r"glosow|głosow|imien", (x.get("name") or ""), re.I)]
        if not atts:
            # pierwszy PDF jako sprawdzenie
            atts = [x for x in (det.get("attachments") or [])
                    if (x.get("extension") or "").lower() == "pdf"]
        if not atts or not date:
            continue
        sessions[aid] = {"id": aid, "roman": roman, "date": date, "title": title,
                         "attach_id": atts[0]["id"]}
    return sessions


def _agg_num(pattern, text):
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    v = m.group(1).strip().replace("o", "0").replace("O", "0")
    return int(v) if v.isdigit() else None


def parse_pdf(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    blocks = re.split(r"\nGłosowanie\s*\n|\nGLOSOWANIE\s*\n", text, flags=re.I)
    votes = []
    for b in blocks[1:]:
        dm = re.search(r"Data głosowania:?\s*([0-9.]+)", b, re.I)
        date = ""
        if dm:
            parts = dm.group(1).strip().split(".")
            if len(parts) == 3:
                d, m, y = parts
                date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        za = _agg_num(r"Głosy\s+za\s+(\d+|o)", b)
        prz = _agg_num(r"Głosy\s+przeciw\s+(\d+|o)", b)
        wst = _agg_num(r"Głosy\s+wstrzymujące\s+się\s+(\d+|o)", b)
        nie = _agg_num(r"Liczba\s+nieobecnych\s+(\d+|o)", b)
        no = _agg_num(r"Obecni\s+niegłosujący\s+(\d+|o)", b)
        t = re.split(r"\nTyp głosowania", b)[0]
        title = " ".join(x.strip() for x in t.split("\n") if x.strip())
        region = b.split("Uprawnieni do głosowania", 1)[1] if "Uprawnieni do głosowania" in b else b
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        for name, tok in _PAIR_RE.findall(region):
            name = _canonical(name)
            tok = re.sub(r"\s+", " ", tok).lower()
            xtok = tok.replace("ł", "l")
            if xtok[:2] == "za":
                named["za"].append(name)
            elif "przeciw" in xtok:
                named["przeciw"].append(name)
            elif "wstrzym" in xtok:
                named["wstrzymal_sie"].append(name)
            elif "nieobecn" in xtok or "niegłosuj" in xtok or "nie glosuj" in xtok or "glosowal" in xtok or "nieoddan" in xtok:
                named["nieobecni"].append(name)
            elif "obecn" in xtok:
                # OBECNY/OBECNA = obecny, oddał głos nieważny (Głosy nieoddane)
                named["brak_glosu"].append(name)
            else:
                named["brak_glosu"].append(name)
        agg = {"za": za, "przeciw": prz, "wstrzym": wst, "nieobecni": nie, "nieoddane": no}
        votes.append({"date": date, "title": title, "agg": agg, "named": named})
    return votes


def _canonical(raw):
    raw = raw.strip().rstrip(".")
    parts = raw.split()
    if len(parts) >= 2:
        return (" ".join(parts[1:]) + " " + parts[0]).strip()
    return raw


def _norm(word):
    n = unicodedata.normalize("NFKD", word.lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    return n


def canonicalize_councilors(records):
    from difflib import SequenceMatcher
    freq = Counter()
    for rec in records:
        for names in rec["named"].values():
            for nm in names:
                freq[nm] += 1
    roster = sorted([nm for nm, c in freq.items() if c >= 10 and len(nm.split()) >= 2],
                    key=lambda x: -freq[x])
    first_name_map = defaultdict(list)
    for nm in roster:
        first_name_map[nm.split()[0].lower()].append(nm)

    def canon(nm):
        for r in roster:
            if r == nm:
                return r
        best, best_sim = None, 0.0
        for r in roster:
            sim = SequenceMatcher(None, _norm(nm), _norm(r)).ratio()
            if sim > best_sim:
                best, best_sim = r, sim
        if best and best_sim >= 0.82:
            return best
        words = nm.split()
        if words:
            cands = first_name_map.get(words[0].lower(), [])
            if len(cands) == 1:
                return cands[0]
            elif len(cands) > 1:
                return max(cands, key=lambda r: freq[r])
            bestf, bestfsim = None, 0.0
            for r in roster:
                fn = r.split()[0]
                s = SequenceMatcher(None, _norm(words[0]), _norm(fn)).ratio()
                if s > bestfsim:
                    bestf, bestfsim = r, s
            if bestf and bestfsim >= 0.6:
                return bestf
        if freq[nm] < 5:
            return None
        return nm

    for rec in records:
        for key in list(rec["named"].keys()):
            seen = set()
            out = []
            for x in (canon(v) for v in rec["named"][key]):
                if x is None or x in seen:
                    continue
                seen.add(x)
                out.append(x)
            rec["named"][key] = out
    return records


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def _rom_num(roman):
    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
    total, prev = 0, 0
    for ch in reversed(roman.upper()):
        v = vals.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total


def collect_all(sessions, cache):
    records = []
    for aid, s in sorted(sessions.items(), key=lambda kv: _rom_num(kv[1]["roman"]) or 99):
        try:
            pdf = gb(f"{API}/files/{s['attach_id']}", cache, s['attach_id'])
        except Exception as e:
            print(f"  [warn] {s['roman']} {s['date']}: {e}")
            continue
        vs = parse_pdf(pdf)
        ok = 0
        for v in vs:
            tn = (len(v["named"]["za"]) + len(v["named"]["przeciw"]) +
                  len(v["named"]["wstrzymal_sie"]) + len(v["named"]["nieobecni"]) +
                  len(v["named"]["brak_glosu"]))
            at = 0
            for k in ("za", "przeciw", "wstrzym", "nieobecni", "nieoddane"):
                if v["agg"].get(k) is not None:
                    at += v["agg"][k]
            if tn == at and tn > 0:
                ok += 1
                r = dict(v)
                r["session_date"] = s["date"]
                r["session_num"] = s["roman"] or s["date"]
                records.append(r)
        print(f"  {s['roman']:6s} {s['date']} ok={ok}/{len(vs)}")
    return records


NAME_AGG = {}
_all_dates = []


def build_output(records):
    all_votes, vid = [], 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
                          "topic": rec.get("title") or "", "named_votes": named,
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
    councilors = {n: {"name": n, "club": "", "district": None, "votes_za": 0, "votes_przeciw": 0,
                      "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
                  for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                if name not in councilors:
                    continue
                c = councilors[name]
                if cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    c["votes_wstrzymal"] += 1
                elif cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1
    total_votes, total_sessions = len(all_votes), len(sessions_data)
    vec = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vec[name][v["id"]] = cat
    councilors_list = []
    for name in sorted(councilors.keys()):
        c = councilors[name]
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        sess_n = len(set(v["session_date"] for v in all_votes
                         if name in (v["named_votes"]["za"] + v["named_votes"]["przeciw"] + v["named_votes"]["wstrzymal_sie"])))
        councilors_list.append({
            "name": name, "club": "", "district": None,
            "frekwencja": round(sess_n / total_sessions * 100, 1) if total_sessions else 0,
            "aktywnosc": round(present / total_votes * 100, 1) if total_votes else 0,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    pairs = []
    ns = sorted(vec.keys())
    for a, b in combinations(ns, 2):
        common = set(vec[a]) & set(vec[b])
        if len(common) < 10:
            continue
        same = sum(1 for x in common if vec[a][x] == vec[b][x])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecny": 0, "brak": 0, "sess": set()})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = ("za" if cat == "za" else "przeciw" if cat == "przeciw"
                       else "wstrzymal_sie" if cat == "wstrzymal_sie"
                       else "nieobecny" if cat == "nieobecni" else "brak")
                cv[name][key] += 1
                if key != "nieobecny":
                    cv[name]["sess"].add(d)
    profiles = []
    n_sessions = len(set(r["session_date"] for r in records)) or 1
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        profiles.append({"name": name, "slug": make_slug(name),
                         "kadencje": {KADENCJA_ID: {
                             "club": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(len(vd["sess"]) / n_sessions * 100, 1),
                             "aktywnosc": round((vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / total * 100, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                             "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
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
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache = Path(args.cache_dir) if args.cache_dir else None
    print("=== Scraper Rada Miejska w Starym Sączu (bip.malopolska.pl, Madkom) ===")
    sessions = collect_sessions(cache)
    print(f"  Sesje IX kadencji z PDF glosowania: {len(sessions)}")
    if not sessions:
        print("  BRAK SESJI."); return 1
    records = collect_all(sessions, cache)
    print(f"  Razem zwalidowanych głosowań: {len(records)}")
    if not records:
        print("  BRAK DANYCH."); return 1
    canonicalize_councilors(records)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    k = output["kadencje"][0]
    print(f"  Sesji: {k['total_sessions']}, głosowań: {k['total_votes']}, radnych: {k['total_councilors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
