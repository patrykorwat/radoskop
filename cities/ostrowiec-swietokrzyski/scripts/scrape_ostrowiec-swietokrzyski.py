#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Ostrowiec Świętokrzyski — imienne głosowania Rady Miasta (IX kadencja).

Źródło: BIP bip.um.ostrowiec.pl (platforma "HIPER/Meg@BIP"-pochodna, API XML kategorii).
Kategoria „Imienne Wykazy Głosowań Rady Miasta" (/artykuly/3103) ma podkategorie roczne
(2024=/artykuly/3276, 2025=/3308, 2026=/3353). Lista artykułów przez /artykuly/xml/{cat}/1/1
(XML z <url>/<tytul>). Każdy artykuł sesji = jedno załącznik-PDF „Wykaz głosowań radnych
na sesji w dn. DD miesiąc YYYY r." pod href /attachments/download/{id}.

PDF = wydruk systemu DSSS Vote: strona 0-1 obecność, potem na każde głosowanie strona z
nagłówkiem 'Uchwała numer "N. temat" ... proporcją głosów: jestem za A, jestem przeciw B,
wstrzymuję się C' + cztery listy imienne w ćwiartkach (L-góra=ZA, P-góra=PRZECIW,
L-dół=WSTRZYMUJĘ SIĘ, P-dół=obecni którzy nie wzięli udziału). Głosowanie może zajmować
kilka stron (ciąg bez agregatu). Walidacja: liczby nazwisk == agregat.

Użycie:
    python scrape_ostrowiec-swietokrzyski.py --city-dir <cities/...> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import ssl
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.request import Request, urlopen

import pymupdf

BIP = "https://bip.um.ostrowiec.pl"
VOTE_CATS = {"3276": 2024, "3308": 2025, "3353": 2026}
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
          "lipca":7,"sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}

REQ_DELAY = 0.3
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir, binary=False):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + (".bin" if binary else ".dat"))
        if cf.is_file():
            data = cf.read_bytes()
            return data if binary else data.decode("utf-8", "ignore")
    _rate()
    data = urlopen(Request(url, headers=_UA), timeout=60, context=CTX).read()
    if cache_dir:
        (Path(cache_dir) / (key + (".bin" if binary else ".dat"))).write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def make_slug(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ---------------- discovery ----------------
def discover_sessions(cache):
    """Artykuły sesji IX kad. z kategorii rocznych (XML)."""
    out = []
    for cat in VOTE_CATS:
        xml = _get(f"{BIP}/artykuly/xml/{cat}/1/1", cache)
        for m in re.finditer(r"<url>(https://bip\.um\.ostrowiec\.pl/artykul/[^<]+)</url>\s*<tytul>([^<]+)</tytul>", xml):
            url, title = m.group(1), m.group(2)
            dm = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", title)
            if not dm:
                continue
            date = f"{dm.group(3)}-{MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
            if date < KAD_START:
                continue
            rm = re.search(r"([IVXL]+)\s+sesja", title, re.I)
            out.append({"url": url, "date": date,
                        "roman": rm.group(1).upper() if rm else "",
                        "title": title})
    # dedup po URL, sort po dacie
    seen = set()
    uniq = []
    for s in sorted(out, key=lambda x: x["date"]):
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        uniq.append(s)
    return uniq


def session_pdf(url, cache):
    """Pobierz jedyny załącznik 'Wykaz głosowań' z artykułu sesji."""
    html = _get(url, cache)
    ids = re.findall(r'href="(https://bip\.um\.ostrowiec\.pl/attachments/download/(\d+))"', html)
    if not ids:
        return None
    return _get(ids[0][0], cache, binary=True)


# ---------------- PDF parsing ----------------
STOP = {"Obecni", "radni,", "radni", "którzy", "nie", "wzięli", "udziału", "w",
        "głosowaniu", "Wstrzymuję", "się", "Jestem", "za", "przeciw", "BRAK",
        "Radni", "zagłosowali", "jak", "poniżej:", "Operatorem", "systemu", "był",
        "Admin.", "Wygenerowano", "DSSS", "Vote", "App.", "z", "oprogramowania",
        "Miasto", "Ostrowiec", "Świętokrzyski"}


def block_quadrants(words):
    """(za[], prz[], wstrz[], neg[]) z jednej strony wydruku DSSS (listy w ćwiartkach)."""
    ys = [w[1] for w in words]
    if not ys:
        return [], [], [], []
    top_h = min((w[1] for w in words if w[4] == "Jestem"), default=280) + 5
    # nagłówek dolnej ćwiartki: kapitalizowane 'Wstrzymuję' (agregat ma małe 'w')
    wstrz_h = next((w[1] for w in words
                    if w[4] == "Wstrzymuję" and w[0] < 250 and top_h + 30 < w[1] < 700), None)
    if wstrz_h is None:
        wstrz_h = max(ys)

    def rows(x0, x1, y0, y1):
        rmap = {}
        for w in words:
            if x0 <= w[0] <= x1 and y0 <= w[1] <= y1:
                rmap.setdefault(round(w[1], 1), []).append((w[0], w[4]))
        out = []
        for y in sorted(rmap):
            toks = [t for _, t in sorted(rmap[y])]
            toks = [t for t in toks if not re.match(r"^\d+\.$", t)]
            if not toks:
                continue
            low = set(t.lower().rstrip(",") for t in toks)
            if low & {s.lower().rstrip(",") for s in STOP}:
                continue
            # nazwisko = same tokeny od wielkiej litery (odrzuciłki typu 'był systemu')
            if any(not re.match(r"^[A-ZŁŚŹŻÓĆĘĄŃ][\wŁłŚśŹźŻżÓóĆćĘęĄąŃń-]*$", t) for t in toks):
                continue
            out.append(" ".join(toks))
        return out

    bot = max((y for y in ys if y < 700), default=max(ys))
    return (rows(60, 300, top_h, wstrz_h - 1), rows(340, 545, top_h, wstrz_h - 1),
            rows(60, 300, wstrz_h + 8, bot), rows(340, 545, wstrz_h + 8, bot))


def parse_vote_pdf(pdf_bytes, session_date, roman):
    """Zwróć listę records {date,num,topic,named,ok} z jednego PDF-a sesji."""
    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    records = []
    cur = None
    for pno, p in enumerate(d):
        words = p.get_text("words")
        text = re.sub(r"\s+", " ", p.get_text())
        agg = re.search(r"jestem za (\d+), jestem przeciw (\d+), wstrzymuję się (\d+)", text)
        tm = re.search(r'Uchwała\s+numer\s+“(.+?)”', text)
        if not tm:
            tm = re.search(r'„(.{10,220}?)”', text)
        title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else None
        za, prz, wstrz, neg = block_quadrants(words)
        if agg:
            if cur is not None:
                records.append(cur)
            named = defaultdict(list)
            named["za"], named["przeciw"], named["wstrzymal_sie"] = za, prz, wstrz
            if neg:
                named["nie_glosowal"] = neg
            cur = {"date": session_date, "num": roman, "topic": title,
                   "named": dict(named),
                   "agg": {"za": int(agg.group(1)), "przeciw": int(agg.group(2)),
                           "wstrzymal_sie": int(agg.group(3))}}
        elif cur is not None and (za or prz or wstrz or neg):
            for cat, lst in (("za", za), ("przeciw", prz), ("wstrzymal_sie", wstrz), ("nie_glosowal", neg)):
                if lst:
                    cur["named"].setdefault(cat, []).extend(lst)
        if title and cur is not None and not cur["topic"]:
            cur["topic"] = title
    if cur is not None:
        records.append(cur)
    for r in records:
        n = {k: len(r["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}
        r["ok"] = all(n[k] == v for k, v in r["agg"].items())
    return records


# ---------------- output building (stargard/lubin pattern) ----------------
def build_output(records, roster, club_assign=None):
    club_assign = club_assign or {}
    sessions_by_date = defaultdict(lambda: {"number": "", "vote_count": 0, "attendees": set()})
    all_votes = []
    vid = 0
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        sessions_by_date[d]["vote_count"] += 1
        if not sessions_by_date[d]["number"]:
            sessions_by_date[d]["number"] = rec.get("num", "")
        named = {k: v for k, v in rec["named"].items() if k in ("za", "przeciw", "wstrzymal_sie")}
        if rec["named"].get("nie_glosowal"):
            named["nie_glosowal"] = rec["named"]["nie_glosowal"]
        for cat in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal"):
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
    all_names = set(roster)
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
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
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
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
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
        same = sum(1 for v_id in common if vectors[a][v_id] == vectors[b][v_id])
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


def build_profiles(records, roster, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nie_glosowal": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    for nm in roster:
        cv.setdefault(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nie_glosowal": 0, "votes": []})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"),
                             "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nie_glosowal"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


ROSTER_URL = f"{BIP}/artykul/62/28303/sklad-rady-miasta"


def fetch_roster(cache):
    html = _get(ROSTER_URL, cache)
    t = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"Skład Rady Miasta:(.*?)Metryczka", t)
    if not m:
        return []
    seg = m.group(1)
    seg = seg.replace(" - ", "-")  # 'Renduda - Dudek' → 'Renduda-Dudek' przed tokenizacją
    seg = re.sub(r"Przewodnicząca Rady Miasta:|Wiceprzewodniczący Rady Miasta:|Radni:", " ; ", seg)
    names = []
    for part in seg.split(";"):
        part = part.strip()
        toks = part.split()
        i = 0
        while i < len(toks):
            if re.match(r"^[A-ZŁŚŹŻÓĆĘĄŃ]", toks[i]) and i + 1 < len(toks) and re.match(r"^[A-ZŁŚŹŻÓĆĘĄŃ]", toks[i + 1]):
                nm = toks[i] + " " + toks[i + 1]
                nm = nm.replace(" - ", "-")
                if _nk(nm) not in (_nk(x) for x in names):
                    names.append(nm)
                i += 2
            else:
                i += 1
    return names


def canon_name(name, canon):
    toks = name.split()
    if len(toks) < 2:
        return name
    key = tuple(sorted(_nk(t) for t in toks))
    return canon.get(key, name)


def merge_orphans(names, canon):
    """Scal rozwinięte na 2 wiersze nazwiska ('Marta' + 'Woźnicka-Kuzdak') jeśli para
    odwzorowuje się na radnego rosteru (token-set)."""
    out = []
    i = 0
    while i < len(names):
        if i + 1 < len(names) and len(names[i].split()) == 1 and len(names[i + 1].split()) == 1:
            key = tuple(sorted((_nk(names[i]), _nk(names[i + 1]))))
            if key in canon:
                out.append(canon[key])
                i += 2
                continue
        out.append(names[i])
        i += 1
    return out


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
    roster = fetch_roster(cache)
    canon = {tuple(sorted(_nk(t) for t in nm.split())): nm for nm in roster if len(nm.split()) >= 2}
    sessions = discover_sessions(cache)
    print(f"[ostrowiec] roster {len(roster)} radnych; {len(sessions)} sesji IX kad.")
    records = []
    bad = 0
    for se in sessions:
        try:
            pdf = session_pdf(se["url"], cache)
            if not pdf:
                print(f"  [skip {se['date']}] brak PDF")
                continue
            recs = parse_vote_pdf(pdf, se["date"], se["roman"])
            for r in recs:
                r["named"] = {cat: [canon_name(nm, canon) for nm in merge_orphans(names, canon)]
                              for cat, names in r["named"].items()}
                # ponowna walidacja po scaleniu rozwiniętych nazwisk
                n = {k: len(r["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}
                r["ok"] = all(n[k] == v for k, v in r["agg"].items())
                if not r.get("ok"):
                    bad += 1
                records.append(r)
            print(f"  [{'ok' if recs else 'skip'}] {se['date']} {se['roman']:>5} votes={len(recs)}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")
    if bad:
        print(f"[ostrowiec] WARNING {bad} głosów bez reconciliacji agregatów")
    output, total_votes, total_sessions = build_output(records, roster, club_assign)
    profiles = build_profiles(records, roster, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ostrowiec] ZAPISANO votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])} bad={bad}")


if __name__ == "__main__":
    main()
