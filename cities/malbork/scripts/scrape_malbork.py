#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Malbork — imienne głosowania Rady Miasta Malborka (IX kadencja 2024-2029).

Źródło: BIP bip.malbork.pl — platforma Madkom "Nowy BIP" (React SPA + API /api/), jw. Kwidzyna.
Odkrywanie: GET /api/menu/657/articles?limit=100 — kategoria "IX RM Imienne głosowania"
(menu 657 pod IX Kadencja 2024-2029 / Rada Miasta). Artykuł = sesja; każdy załącznik PDF
to osobny raport głosowania systemu kongresowego Deputy: "RAPORT PRZEPROWADZONEGO GŁOSOWANIA".
Pobieranie: GET /api/articles/{id} -> attachments[] -> GET /e,pobierz,get.html?id={attId}.

Format PDF (1 strona = 1 głosowanie):
  Nazwa sesji: XXXI Sesja Rady Miasta Malborka
  Data głosowania: 24.06.2026
  Temat głosowania: <temat>            (może się łamać do "Typ głosowania:")
  Typ głosowania: …  Typ wyniku: …
  Uprawnionych razem: N  Głosów ZA: n
  Uprawnionych obecnych: N  Głosów WSTRZ: n
  Głosujących: N  Głosów PRZECIW: n
  Głosowanie jawne: TAK
  Uchwała [nie ]została podjęta
  Głosy indywidualne:
  Lp. Imię i Nazwisko Głos
  1 Jacek Markowski NIE
  2 Marek Charzewski brak uprawnień      <- nie-radny/nie głosuje; pomijany
  …
  System kongresowy Deputy. Data wydruku raportu: …

Głosy: TAK=za, NIE=przeciw, WST=wstrzymal_sie; "brak uprawnień" = poza składem
(burmistrz, "Mównica") — pomijane. Nazwiska już "Imię Nazwisko".
Walidacja per głos: policzone == agregaty ZA/WSTRZ/PRZECIW. Tylko zwalidowane.
Skład DYNAMICZNY: unikalne nazwiska, które oddały głos.

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python scrape_malbork.py --city-dir <dir> [--cache-dir dir]
"""
import argparse
import hashlib
import io
import json
import re
import ssl
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.malbork.pl"
MENU_ID = "657"  # IX RM Imienne głosowania
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.4
_LAST = 0.0

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


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


def _get(url, cache_dir, binary=False, tries=4):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + (".bin" if binary else ".txt"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    data = b""
    for i in range(tries):
        try:
            _rate()
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"})
            data = urllib.request.urlopen(req, timeout=60, context=_CTX).read()
            break
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + 2 * i)
    if cache_dir:
        (cache_dir / (key + (".bin" if binary else ".txt"))).write_bytes(data)
    return data if binary else data.decode("utf-8", "replace")


def _getj(url, cache_dir):
    return json.loads(_get(url, cache_dir))


# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """Artykuły sesji z menu 'IX RM Imienne głosowania'."""
    arts = []
    offset = 0
    while True:
        d = _getj(f"{BIP}/api/menu/{MENU_ID}/articles?limit=100&offset={offset}", cache_dir)
        els = d.get("articles") or d.get("elements") or []
        for e in els:
            arts.append({"id": e["id"]})
        if len(els) < 100 or len(arts) >= d.get("total", 0):
            break
        offset += 100
    return arts


_ROM = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
def _rom2int(s):
    tot, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROM.get(ch, 0)
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


# ---------------- PDF parsing ----------------
_DATE_RE = re.compile(r"^Data g\S+owania:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.M)
_SESJA_RE = re.compile(r"^Nazwa sesji:\s*(.+)$", re.M)
_TOPIC_RE = re.compile(r"^Temat g\S+owania:\s*(.*?)(?=\nTyp g\S+owania:)", re.M | re.S)
_ZA_RE = re.compile(r"G\S+os\w*\s+ZA:\s*(\d+)")
_WSTR_RE = re.compile(r"G\S+os\w*\s+WSTRZ:\s*(\d+)")
_PRZ_RE = re.compile(r"G\S+os\w*\s+PRZECIW:\s*(\d+)")
_WIN_RE = re.compile(r"Uchwała (nie zosta\S+ podj\S+ta|zosta\S+ podj\S+ta)")
_ROW_RE = re.compile(r"^(\d{1,2})[.\s]\s*(\S.*)$")

_NON_PERSON = re.compile(r"mownica|mównic", re.I)


def _norm_vote(rest):
    r = _nk(rest)
    if r == "tak":
        return "za"
    if r == "nie":
        return "przeciw"
    if r.startswith("wstrz"):
        return "wstrzymal_sie"
    if r.startswith("nieglos"):
        return "nieglosowal"
    if "brakuprawnien" in r:
        return "poza"
    if r.startswith("nieobecn"):
        return "nieobecni"
    return None


def _parse_rows(tail):
    rows = {}
    tl = tail.splitlines()
    start = 0
    for i, l in enumerate(tl):
        if ("Lp" in l or l.strip().startswith("Nr")) and ("G" in l or "os" in l):
            start = i + 1
            break
    for line in tl[start:]:
        line = line.strip()
        if "kongresowy" in line.lower() or "Nazwisko" in line:
            break
        m = _ROW_RE.match(line)
        if not m:
            continue
        lp = int(m.group(1))
        if not (1 <= lp <= 60) or lp in rows:
            continue
        parts = m.group(2).split()
        vote = None
        name_parts = parts
        for k in range(len(parts) - 1, max(0, len(parts) - 4), -1):
            cand = " ".join(parts[k:])
            vote = _norm_vote(cand)
            if vote is not None:
                name_parts = parts[:k]
                break
        if vote is None:
            continue
        name = " ".join(name_parts).strip().strip(".-–—").strip()
        name = re.sub(r"^\W+", "", name)
        if len(name.split()) < 2 or _NON_PERSON.search(name):
            continue
        rows[lp] = (name, vote)
    return rows


def _finalize(v, rows):
    counter = Counter(c for _, c in rows.values())
    ok = (v["agg"]["za"] is not None and v["agg"]["przeciw"] is not None and v["agg"]["wstrzym"] is not None
          and counter.get("za", 0) == v["agg"]["za"]
          and counter.get("przeciw", 0) == v["agg"]["przeciw"]
          and counter.get("wstrzymal_sie", 0) == v["agg"]["wstrzym"])
    named = defaultdict(list)
    for _, (name, cat) in sorted(rows.items()):
        if cat != "poza":
            named[cat].append(name)
    v["named"] = dict(named)
    v["ok"] = ok
    v["counter"] = dict(counter)
    return v


def parse_report(data):
    """PDF raportu (1 lub wiele głosowań, kontynuacje tabeli na dalszych stronach) -> lista głosów."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    full = "\n".join(pages)
    if "RAPORT" not in full[:200].upper():
        return []
    parts = re.split(r"(?=^RAPORT PRZEPROWADZONEGO G\u0141OSOWANIA)", full, flags=re.M)
    votes = []
    for chunk in parts:
        if "RAPORT" not in chunk.upper() or ("G\u0142osy indywidualne" not in chunk and "Glosy indywidualne" not in chunk):
            continue
        gi = chunk.find("G\u0142osy indywidualne")
        if gi < 0:
            gi = chunk.find("Glosy indywidualne")
        rows = _parse_rows(chunk[gi:])
        mdate = _DATE_RE.search(chunk)
        if mdate and not (mza0 := (_ZA_RE.search(chunk) or _WSTR_RE.search(chunk) or _PRZ_RE.search(chunk))) and votes:
            # kontynuacja tabeli: data jest, agregatów brak
            votes[-1]["rows"].update({lp: nv for lp, nv in rows.items() if lp not in votes[-1]["rows"]})
            continue
        if mdate:
            date = f"{mdate.group(3)}-{int(mdate.group(2)):02d}-{int(mdate.group(1)):02d}"
            ms = _SESJA_RE.search(chunk)
            sesja = re.sub(r"\s+", " ", ms.group(1)).strip() if ms else ""
            mnum = re.match(r"([IVXLCDM]+)", sesja)
            num = _rom2int(mnum.group(1)) if mnum else 0
            mt = _TOPIC_RE.search(chunk)
            topic = re.sub(r"\s+", " ", mt.group(1)).strip() if mt else ""
            mza, mw, mp = _ZA_RE.search(chunk), _WSTR_RE.search(chunk), _PRZ_RE.search(chunk)
            agg = {"za": int(mza.group(1)) if mza else None,
                   "wstrzym": int(mw.group(1)) if mw else None,
                   "przeciw": int(mp.group(1)) if mp else None}
            mwin = _WIN_RE.search(chunk)
            result = ""
            if mwin:
                result = "odrzucono" if mwin.group(1).startswith("nie") else "przyjete"
            votes.append({"date": date, "num": num, "topic": topic, "result": result,
                          "agg": agg, "rows": rows})
        elif votes:
            votes[-1]["rows"].update({lp: nv for lp, nv in rows.items() if lp not in votes[-1]["rows"]})
    return [_finalize(v, v.pop("rows")) for v in votes]


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


# ---------------- output ----------------
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
                                   "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": rec["named"],
                          "counts": {k: len(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    councilors_data = {}
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                councilors_data.setdefault(nm, {"name": nm, "club": club_assign.get(nm, "NZ"),
                    "district": None, "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                    "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                key = {"za": "votes_za", "przeciw": "votes_przeciw",
                       "wstrzymal_sie": "votes_wstrzymal", "nieobecni": "votes_nieobecny",
                       "nieglosowal": "votes_brak"}.get(cat)
                if key:
                    councilors_data[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
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
    pairs = []
    for a, b in combinations(sorted(vectors), 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for x in common if vectors[a][x] == vectors[b][x])
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
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "nieglosowal": 0, "votes": []})
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
    for nm in sorted(cv):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    arts = discover_sessions(cache)
    print(f"[malbork] {len(arts)} artykułów sesji w 'IX RM Imienne głosowania'")

    records = []
    seen_votes = set()
    vstat = {"v": 0, "ok": 0, "fail": 0}
    for a in arts:
        try:
            art = _getj(f"{BIP}/api/articles/{a['id']}", cache)
        except Exception as e:
            print(f"  [ERR art {a['id']}] {e}")
            continue
        atts = [x for x in (art.get("attachments") or []) if x.get("extension") == "pdf"]
        nok = 0
        for att in atts:
            try:
                data = _get(f"{BIP}/e,pobierz,get.html?id={att['id']}", cache, binary=True)
                vs = parse_report(data)
            except Exception as e:
                print(f"    [ERR att {att['id']}] {type(e).__name__}: {e}")
                continue
            for v in vs:
                vstat["v"] += 1
                if not v["ok"]:
                    vstat["fail"] += 1
                    print(f"    ! FAIL date={v['date']} agg={v['agg']} counter={v['counter']} topic={v['topic'][:50]}")
                    continue
                key = (v["date"], v["topic"][:80])
                if key in seen_votes:
                    continue
                seen_votes.add(key)
                vstat["ok"] += 1
                nok += 1
                records.append({"date": v["date"], "num": v["num"], "topic": v["topic"], "named": v["named"]})
        print(f"  [art {a['id']}] {art.get('title','')[:60]} -> raporty OK={nok}/{len(atts)}")

    records.sort(key=lambda r: r["date"])
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
    print(f"[malbork] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])} validated={vstat['ok']}/{vstat['v']} fail={vstat['fail']}")


if __name__ == "__main__":
    main()
