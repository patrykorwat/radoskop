#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Kwidzyn — imienne głosowania Rady Miejskiej w Kwidzynie (IX kadencja 2024-2029).

Źródło: BIP bip.kwidzyn.pl — platforma Madkom "Nowy BIP" (React SPA + API /api/).
Odkrywanie: GET /api/contexts/default/articles?limit=200&offset=N (pełny wykaz artykułów BIP,
~8500 pozycji). Artykuły "Imienny wykaz głosowań … Sesji Rady Miejskiej w Kwidzynie w dniu …".
Każdy artykuł: GET /api/articles/{id} -> attachments[0] -> PDF: GET /api/files/{attachment_id}.

Format PDF (tekstowy, wydruk z systemu, jedna sesja = jeden PDF, wiele głosów w dokumencie):
  nagłówek bloku głosu: "<pp.p>. <temat>;" / "głosowanie <temat>;" / "jednostka …" /
    "wynik Głosowanie zakończone wynikiem: przyjęto|odrzucono" / "data <miesiąc> <D> r." /
    "typ głosowanie jawne imienne …" / sekcja Podsumowanie:
    "ZA <n> <proc> %", "PRZECIW <n> …", "WSTRZYMAŁO[ SIĘ] <n> …", pula/oddane/nieoddane;
  tabela imienna po "Wyniki imienne" + "lp nazwisko imię głos":
    wiersze "<lp> <Nazwisko> <Imię> <GŁOS>", głos ∈ {ZA, PRZECIW, WSTRZYMAŁ SIĘ/WSTRZYMAŁA SIĘ,
    nieobecny/nieobecna, nie głosowała/-ł}. Nazwiska dwuczłonowe ("Górska - Moch Patrycja").
  Podział strony przerywa tabelę — wiersze kontynuacji po markecie strony.

Walidacja per głos: policzone ZA/PRZECIW/WSTRZYMAŁO == agregaty Podsumowanie; liczba wierszy
imiennych == pula głosów (radni 21). Tylko zwalidowane głosy trafiają do wyjścia.

Obrót nazwisk: źródło "Nazwisko Imię" -> Radoskop "Imię Nazwisko".
Skład DYNAMICZNY: roster = unikalne nazwiska ze zwalidowanych głosów IX kadencji.

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python scrape_kwidzyn.py --city-dir <dir> [--cache-dir dir]
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pdfplumber
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import ssl
import urllib.request

BIP = "https://bip.kwidzyn.pl"
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
_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}

_TITLE_RE = re.compile(
    r"^imienny\s+wykaz\s+g\s*[lł]osowa\w*\s+(?:radnych\s+)?podczas\s+(?:obrad\s+)?"
    r"([IVXLCDM]+)(?:\s+(?:nadzwyczajnej|Nadzwyczajnej))?\s*[Ss]esji", re.I)


def discover_articles(cache_dir):
    """Wszystkie artykuły sesji imiennych: {id, title, pubdate} (dedup po id)."""
    found = {}
    offset, total = 0, 10 ** 9
    while offset < total:
        d = _getj(f"{BIP}/api/contexts/default/articles?limit=200&offset={offset}", cache_dir)
        total = d.get("total", 0)
        for e in d.get("elements", []):
            t = e.get("title") or ""
            tt = _nk(t)
            if "imienn" in tt and "glosow" in tt and "sesj" in tt and _TITLE_RE.search(t.strip()):
                found[e["id"]] = {"id": e["id"], "title": t.strip(),
                                  "pub": (e.get("date") or "")[:10]}
        offset += 200
    return list(found.values())


_DATE_RE = re.compile(r"w\s+dniu\s+(\d{1,2})\s*(?:-\s*\d{1,2}\s+)?(\w+)\s+(\d{4})", re.I)


def _session_date(title):
    m = _DATE_RE.search(title)
    if not m:
        return None
    mo = _MONTHS.get(_nk(m.group(2)))
    if not mo:
        return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"


_ROM = {}
def _rom2int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    s = s.upper()
    tot = 0
    prev = 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


# ---------------- PDF parsing ----------------
_AGG_RE = re.compile(r"^(ZA|PRZECIW|WSTRZYMA\u0141O)(?:\s+SI\u0118)?\s+(\d+)\s", re.M)
_TOPIC_RE = re.compile(r"^(\d+\.\d+)\.\s+(.*?);", re.M | re.S)
_VOTE_LINE_RE = re.compile(r"^(\d{1,2})\s+(\S.*)$")
# wariant B (sesje I..X, pocz. IX kad.): nagłówek "KWIDZYN, dn.: … r." + "GŁOSOWAŁO: n"
_HDR_B_RE = re.compile(r"^KWIDZYN, dn\.?:", re.M)
_AGG_B_RE = re.compile(r"g\s*losowa\s*\u0142\s*o\s*:\s*(\d+)|g\s*losowa\s*\u0142o\s+ZA\s*:\s*(\d+)|g\s*losowa\s*\u0142o\s+PRZECIW\s*:\s*(\d+)|WSTRZYMA\s*\u0141O\s+si\s*\u0119\s*:\s*(\d+)", re.I)


def _norm_vote(rest):
    r = _nk(rest)
    if r == "za":
        return "za"
    if r.startswith("przeciw"):
        return "przeciw"
    if r.startswith("wstrzym"):
        return "wstrzymal_sie"
    if r.startswith("nieobecn"):
        return "nieobecni"
    if r.startswith("nieglosowal") or r.startswith("nieglosowala"):
        return "nieglosowal"
    if r.startswith("obecn"):
        return "obecny"
    return None


def _rows_from_lines(lines, out):
    for line in lines:
        line = line.strip()
        m = _VOTE_LINE_RE.match(line)
        if not m:
            continue
        lp = int(m.group(1))
        if not (1 <= lp <= 60):
            continue
        rest = m.group(2).strip()
        parts = rest.split()
        vote_cat = None
        name_parts = parts
        for k in range(len(parts) - 1, 0, -1):
            cand = " ".join(parts[k:])
            vote_cat = _norm_vote(cand)
            if vote_cat is not None:
                name_parts = parts[:k]
                break
        if vote_cat is None:
            continue
        name = " ".join(name_parts).strip()
        name = re.sub(r"\s*[-–—]\s*", " ", name).strip()
        if len(name.split()) < 2 or len(re.sub(r"[^A-Za-z]", "", name)) < 4:
            continue
        out.append((lp, name, vote_cat))


def _parse_votes_format_a(block):
    """Wariant A: '<pp.pp>. temat;' + Podsumowanie ZA/PRZECIW/WSTRZYMAŁO + Wyniki imienne."""
    mt = _TOPIC_RE.match(block)
    topic = re.sub(r"\s+", " ", mt.group(2)).strip() if mt else ""
    magg = {m.group(1): int(m.group(2)) for m in _AGG_RE.finditer(block)}
    agg = {"za": magg.get("ZA"), "przeciw": magg.get("PRZECIW"),
           "wstrzym": magg.get("WSTRZYMA\u0141O")}
    wi = block.rfind("Wyniki imienne")
    rows = []
    if wi >= 0:
        _rows_from_lines(block[wi:].splitlines(), rows)
    return topic, agg, rows


def _parse_votes_format_b(block):
    """Wariant B: 'KWIDZYN, dn.: …' + GŁOSOWAŁO/głosowało ZA… + LP. Nazwisko i Imię jak głosował."""
    lines = block.splitlines()
    # topic: lines po KWIDZYN aż do 'Załącznik'
    topic = ""
    mkn = re.search(r"^KWIDZYN, dn\.?:.*$", block, re.M)
    if mkn:
        tail = block[mkn.end():]
        mza = re.search(r"^Za\u0142\u0105cznik.*$", tail, re.M)
        topic = re.sub(r"\s+", " ", tail[:mza.start()] if mza else tail[:200]).strip()
    nums = _AGG_B_RE.findall(block)
    glowalo = za = przeciw = wstrz = None
    for g, z, p, w in nums:
        if g:
            glowalo = int(g)
        elif z:
            za = int(z)
        elif p:
            przeciw = int(p)
        elif w:
            wstrz = int(w)
    agg = {"za": za, "przeciw": przeciw, "wstrzym": wstrz, "glowalo": glowalo}
    rows = []
    _rows_from_lines(lines, rows)
    return topic, agg, rows


def _finish_vote(topic, agg, rows, block):
    bylp = {}
    for lp, name, cat in rows:
        if lp not in bylp:
            bylp[lp] = (name, cat)
    counter = Counter(c for _, c in bylp.values())
    names = {}
    for name, cat in bylp.values():
        names.setdefault(cat, []).append(name)
    ok = (agg["za"] is not None and agg["przeciw"] is not None and agg["wstrzym"] is not None
          and counter.get("za", 0) == agg["za"]
          and counter.get("przeciw", 0) == agg["przeciw"]
          and counter.get("wstrzymal_sie", 0) == agg["wstrzym"]
          and (agg.get("glowalo") is None or sum(counter.get(k, 0) for k in ("za", "przeciw", "wstrzymal_sie")) == agg["glowalo"]))
    mwin = re.search(r"wynik\s+G\s*losowanie zako\u0144czone wynikiem:\s*(\S+)", block)
    return {"topic": topic, "result": mwin.group(1) if mwin else "",
            "named": {"za": names.get("za", []), "przeciw": names.get("przeciw", []),
                      "wstrzymal_sie": names.get("wstrzymal_sie", []),
                      "nieobecni": names.get("nieobecni", [])},
            "agg": agg, "ok": ok, "n_rows": len(bylp), "counter": dict(counter)}


def parse_pdf(data):
    """Jeden PDF sesji -> list głosów {topic, named, agg, ok}. Obsługa wariantów A i B."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    full = "\n".join(pages)
    idxs_a = [m.start() for m in re.finditer(r"^\d+\.\d+\.\s", full, re.M)]
    idxs_b = [m.start() for m in _HDR_B_RE.finditer(full)]
    votes = []
    if len(idxs_a) >= len(idxs_b) and idxs_a:
        for n, st in enumerate(idxs_a):
            en = idxs_a[n + 1] if n + 1 < len(idxs_a) else len(full)
            block = full[st:en]
            topic, agg, rows = _parse_votes_format_a(block)
            votes.append(_finish_vote(topic, agg, rows, block))
    elif idxs_b:
        for n, st in enumerate(idxs_b):
            en = idxs_b[n + 1] if n + 1 < len(idxs_b) else len(full)
            block = full[st:en]
            topic, agg, rows = _parse_votes_format_b(block)
            votes.append(_finish_vote(topic, agg, rows, block))
    return votes


def _flip(name):
    """'Nazwisko Imię' -> 'Imię Nazwisko' (obsługa 3+ wyrazów: Nazwisko1 Nazwisko2 Imię)."""
    parts = name.split()
    if len(parts) >= 3 and parts[-1][0].isupper() and parts[0][0].isupper():
        # heurystyka: imię = ostatni wyraz; reszta nazwisko
        return " ".join(parts[1:] + parts[:1]) if len(parts) == 2 else " ".join([parts[-1]] + parts[:-1])
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return name


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
                                   "attendees": set(), "speakers": []}
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
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm]["votes_wstrzymal"] += 1
                elif cat == "nieobecni":
                    councilors_data[nm]["votes_nieobecny"] += 1
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
    from itertools import combinations
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

    arts = discover_articles(cache)
    sess = []
    for a in arts:
        d = _session_date(a["title"])
        m = _TITLE_RE.match(a["title"])
        num = _rom2int(m.group(1)) if m else 0
        if d and d >= KAD_START:
            sess.append({"id": a["id"], "date": d, "num": num, "title": a["title"]})
    sess.sort(key=lambda x: x["date"])
    print(f"[kwidzyn] {len(sess)} sesji IX kad. z imiennymi wykazami")

    records = []
    vstat = {"v": 0, "ok": 0, "fail": 0}
    for s in sess:
        try:
            art = _getj(f"{BIP}/api/articles/{s['id']}", cache)
            atts = [x for x in (art.get("attachments") or []) if x.get("extension") == "pdf"]
            if not atts:
                print(f"  [skip {s['date']}] brak PDF")
                continue
            data = _get(f"{BIP}/api/files/{atts[0]['id']}", cache, binary=True)
            votes = parse_pdf(data)
            nok = sum(1 for v in votes if v["ok"])
            vstat["v"] += len(votes)
            vstat["ok"] += nok
            vstat["fail"] += len(votes) - nok
            for v in votes:
                if not v["ok"]:
                    continue
                v["named"] = {k: sorted({_flip(n) for n in ns}) for k, ns in v["named"].items()}
                records.append({"date": s["date"], "num": s["num"], "topic": v["topic"],
                                "named": v["named"]})
            flag = "OK" if nok == len(votes) else f"VALID={nok}/{len(votes)}"
            print(f"  [ok {s['date']}] sesja {s['num']} votes={len(votes)} {flag}")
            for v in votes:
                if not v["ok"]:
                    print(f"     ! FAIL agg={v['agg']} rows={v['n_rows']} counter={v['counter']} topic={v['topic'][:60]}")
        except Exception as e:
            print(f"  [ERR {s['date']}] {type(e).__name__}: {e}")

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
    print(f"[kwidzyn] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])} validated={vstat['ok']}/{vstat['v']} fail={vstat['fail']}")


if __name__ == "__main__":
    main()
