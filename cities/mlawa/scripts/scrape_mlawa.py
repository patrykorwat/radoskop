#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Mława — imienne głosowania Rady Miasta Mława (IX kadencja 2024-2029).

Źródło: BIP UM Mława (bip.mlawa.pl, CMS Drupal), kategoria
"/artykuly/imienne-wykazy-glosowan-radnych-0" (paginowana, 3 strony).
Per sesja jest artykuł "N Sesja Rady Miasta Mława z dnia ..." z ZAŁĄCZNIKAMI PDF
("Raport z głosowań" / "Imienne głosowania" / "Imienny wykaz głosowań" — nazewnictwo
niestałe). Każdy PDF to raport eSesja z blokami "Wyniki głosowania (Radni)" →
agregat (ZA/PRZECIW/WSTRZYMUJĘ SIĘ/BRAK GŁOSU/NIEOBECNI) + "Wyniki imienne" z listami
radnych per kategoria. Występują 4 warianty formatu (A: numerowane + "Głosowano w
sprawie X"; B: "Wyniki głosowania (Radni)" + sub-głosowania z osobnym agregatem;
C: "Pkt. N. ... Wykaz głosowania:" z nazwiskami w osobnych wierszach i bez agregatu;
D: jak A ale bez numeracji). Parsowane pdfplumber; nazwiska odtwarzane przez
dopasowanie do kanonicznego rosteru radnych (odporne na zawijanie wierszy, numery
stron i ułamane tytuły).

Zakres: 27 sesji (III..XXIX, 2024-06-11..2026-08-25; pominięto I - inauguracyjna bez
głosowań, II - brak publikacji wykazu), 440 głosowań, 21 aktywnych radnych (+3 byłe
mandaty z 2024: Burchacki/Zejer/Szczechowicz, wliczani gdy mają głosy). Sesje XV -
tylko wariant C (bez agregatu do walidacji). Radni zweryfikowani z BIP "Skład Rady
Miasta 2024-2029". Kluby nie są publikowane w BIP -> club="".

Użycie:
    python scrape_mlawa.py --output docs/data.json --profiles docs/profiles.json
                            [--config config.json] [--cache-dir CACHE]
"""
import argparse
import io
import json
import os
import re
import time
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import urllib.request

BASE = "https://bip.mlawa.pl"
VOTES_CAT = "/artykuly/imienne-wykazy-glosowan-radnych-0"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0; research)"}

# Kanoniczna lista aktywnych radnych IX kadencji (BIP "Skład Rady Miasta 2024-2029").
ROSTER_MAIN = ["Andrzej Karpiński", "Arkadiusz Dłubisz", "Bożena Ryska", "Filip Kowalczyk",
               "Grzegorz Komur", "Jacek Sych", "Janusz Wojnarowski", "Kamil Przybyszewski",
               "Marek Kiełbiński", "Mariusz Dziubiński", "Mirosław Zbrzezny", "Patryk Fabisiak",
               "Paweł Łubiński", "Paweł Majewski", "Ryszard Prusinowski", "Sławomir Kowalewski",
               "Szymon Wyrostek", "Tadeusz Stabach", "Wojciech Franciszek Krajewski",
               "Zbigniew Korczak", "Zbigniew Ruszkowski"]
ROSTER_FORMER = ["Mariusz Szczechowicz", "Szymon Zejer", "Marcin Burchacki"]
ROSTER = ROSTER_MAIN + ROSTER_FORMER
NAME_MAP = {n: (n, False) for n in ROSTER_MAIN}
for n in ROSTER_FORMER:
    NAME_MAP[n] = (n, True)
# Formy pełne ze składu -> nazwa skrócona z PDF.
for full, short in [("Filip Tomasz Kowalczyk", "Filip Kowalczyk"),
                    ("Kamil Robert Przybyszewski", "Kamil Przybyszewski"),
                    ("Tadeusz Andrzej Stabach", "Tadeusz Stabach")]:
    NAME_MAP[full] = (short, False)
_pat = sorted(NAME_MAP.keys(), key=lambda s: -len(s))
NAME_RE = re.compile("|".join(re.escape(n) for n in _pat))

CAT_RE = re.compile(r"^(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\((\d+)[^)]*\)\s*$")
AGG_RE = re.compile(r"^ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)\s*$")
NUM_RE = re.compile(r"^(\d{1,3})\.\s+(.*)$")
PKT_RE = re.compile(r"^Pkt\.\s*(\d+[a-z]?)\.?\s*(.*)$")

VKEY = ["osow", "wykaz", "imienne", "imienny", "raport", "protok"]

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
         "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
         "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
         "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
         "XXVIII": 28, "XXIX": 29}
ROMAN_REV = {v: k for k, v in ROMAN.items()}

CLUB_ASSIGN = None


def _norm_ws(s):
    return " ".join(s.split())


def _match_names(segment):
    seg = _norm_ws(segment)
    return [NAME_MAP[m.group(0)][0] for m in NAME_RE.finditer(seg)]


def _fetch(url, cache_dir, timeout=40):
    if cache_dir:
        import hashlib
        _h = hashlib.md5(url.encode()).hexdigest()[:16]
        _ext = ".bin" if url.lower().endswith(".pdf") else ".html"
        fp = Path(cache_dir) / (url.split("/")[-1][:40] + "_" + _h + _ext)
        if fp.exists():
            return fp.read_bytes()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            r = urllib.request.urlopen(req, timeout=timeout)
            data = r.read()
            if cache_dir:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            return data
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url)


def _html(url, cache_dir):
    return _fetch(url, cache_dir).decode("utf-8", "replace")


def _clean_lines(pdf_lines):
    out = []
    for l in pdf_lines:
        s = l.strip()
        if not s:
            continue
        if re.fullmatch(r"\d{1,3}", s):
            continue
        if "Wygenerowano za pomocą app.esesja.pl" in s:
            continue
        out.append(s)
    return out


def _extract_lines(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [ln.rstrip() for pg in pdf.pages for ln in (pg.extract_text() or "").split("\n")]


def _parse_pdf(data):
    """Zunifikowany parser 4 wariantów formularza Mławy.
    Zwraca listę {topic, agg, cats:{ZA:[...],PRZECIW:...,WSTRZYMUJĘ SIĘ:...,BRAK GŁOSU:...,NIEOBECNI:...}}."""
    lines = _clean_lines(_extract_lines(data))
    n = len(lines)
    has_agg = any(AGG_RE.match(l) for l in lines)
    is_C = (not has_agg) and any("Wykaz głosowania:" in l for l in lines)
    votes = []
    vote = None
    cur_cat = None
    region = []
    cur_point = ""
    sprawa_buf = None

    def emit():
        nonlocal cur_cat, region
        if vote is not None and cur_cat is not None:
            vote["cats"][cur_cat] = _match_names(" ".join(region))
        cur_cat = None
        region = []

    def new_vote():
        nonlocal vote, cur_cat, region, sprawa_buf
        emit()
        vote = {"topic": cur_point, "agg": None, "cats": {}}
        votes.append(vote)
        sprawa_buf = None

    i = 0
    while i < n:
        l = lines[i]
        if is_C:
            pm = PKT_RE.match(l)
            if pm:
                new_vote()
                cur_point = pm.group(2).strip()
                votes[-1]["topic"] = cur_point
                i += 1
                continue
            if "Wykaz głosowania:" in l:
                if vote is None:
                    new_vote()
                i += 1
                continue
        else:
            am = AGG_RE.match(l)
            if am:
                new_vote()
                vote["agg"] = tuple(map(int, am.groups()))
                i += 1
                continue
            nm = NUM_RE.match(l)
            if nm and vote is not None:
                if vote["cats"]:
                    new_vote()
                cur_point = nm.group(2).strip()
                g = re.match(r"^Głosowano w sprawie\s*:?\s*(.*)$", cur_point)
                if g:
                    cur_point = g.group(1).strip()
                vote["topic"] = cur_point
                i += 1
                continue
            gs = re.match(r"^Głosowano w sprawie\s*:?\s*(.*)$", l)
            if gs:
                rest = gs.group(1).strip()
                if rest:
                    cur_point = rest
                    if vote is not None and vote["agg"] is None and not vote["cats"]:
                        vote["topic"] = rest
                    sprawa_buf = None
                else:
                    sprawa_buf = []
                i += 1
                continue
            if sprawa_buf is not None:
                if AGG_RE.match(l) or CAT_RE.match(l) or "Wyniki imienne" in l or "Wyniki głosowania" in l:
                    cur_point = " ".join(sprawa_buf)
                    if vote is not None:
                        vote["topic"] = cur_point
                    sprawa_buf = None
                else:
                    sprawa_buf.append(l)
                    i += 1
                    continue
        hm = CAT_RE.match(l)
        if hm:
            if vote is None:
                new_vote()
            emit()
            cur_cat = hm.group(1)
            region = []
            i += 1
            continue
        if cur_cat is not None:
            if (AGG_RE.match(l) or CAT_RE.match(l) or "Wyniki głosowania" in l or NUM_RE.match(l)
                    or PKT_RE.match(l) or "Głosowano w sprawie" in l or "Wyniki imienne" in l):
                emit()
                continue
            region.append(l)
            i += 1
            continue
        i += 1
    emit()
    if not is_C:
        votes = [v for v in votes if v["agg"] is not None]
    return votes


def _session_date_from_title(title):
    m = re.search(r"z dnia (\d{1,2})\s+([a-ząćężźćśńłó]+)\s+(\d{4})", title)
    if m:
        d, mo, y = m.group(1), m.group(2).lower(), m.group(3)
        if mo in MONTHS:
            return f"{y}-{MONTHS[mo]:02d}-{int(d):02d}"
    return None


def _roman_from_title(title):
    m = re.match(r"^\s*([IVXLC]+)\s+Sesja", title)
    return m.group(1) if m else None


def _voting_pdf_hrefs(article_html):
    """Wszystkie linki do PDF-ów głosowań w artykule sesji (zdekodowane nazwy)."""
    hrefs = []
    for a in re.findall(r'<a[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>', article_html, re.I):
        dh = urllib.parse.unquote(a).lower()
        if "uchwala" in dh:
            continue
        if any(k in dh for k in VKEY):
            full = a if a.startswith("http") else BASE + a
            if full not in hrefs:
                hrefs.append(full)
    return hrefs


def _harvest(cache_dir):
    """Sesje IX kadencji z kategorii -> [{roman, date, url, pdf_hrefs}]."""
    sessions = []
    seen = set()
    for page in (0, 1, 2):
        url = f"{BASE}{VOTES_CAT}?page={page}" if page else BASE + VOTES_CAT
        html = _html(url, cache_dir)
        for a in re.findall(r'<a[^>]*href=["\'](/artykul/[^"\']+)["\'][^>]*>(.*?)</a>', html, re.S):
            href, inner = a
            txt = _norm_ws(re.sub(r"<[^>]+>", " ", inner))
            m = re.match(r"^([IVXLC]+) Sesja Rady Miasta Mława z dnia (\d{1,2}) (\w+) (\d{4})", txt)
            if not m:
                continue
            roman, d, mo, y = m.group(1), int(m.group(2)), m.group(3).lower(), int(m.group(4))
            if mo not in MONTHS:
                continue
            date = f"{y}-{MONTHS[mo]:02d}-{d:02d}"
            if href in seen:
                continue
            seen.add(href)
            sessions.append({"roman": roman, "date": date, "url": BASE + href})
        if re.search(r"\?page=2", html):
            pass
    # sessions bez wykazu głosowań (I inauguracyjna, II bez publikacji) są pomijane
    return sessions


def _club_of(name):
    if CLUB_ASSIGN:
        return CLUB_ASSIGN.get(name, "")
    return ""


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def _best_pdf_votes(data_list):
    """Wybierz najpełniejszy raport (najwięcej głosowań z agregatami; fallback najwięcej głosowań)."""
    best = None
    for data in data_list:
        vs = _parse_pdf(data)
        nagg = sum(1 for v in vs if v["agg"])
        key = (nagg, len(vs)) if nagg > 0 else (0, len(vs))
        if best is None or key > best[0]:
            best = (key, vs)
    return best[1] if best else []


def build_output(records):
    """records: [{date, roman, votes:[{topic, agg, cats}]}]"""
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if d not in sessions_by_date:
            num = rec.get("roman") or ""
            sessions_by_date[d] = {"date": d, "number": num, "vote_count": 0, "attendees": set()}
        for raw in rec["votes"]:
            cats = raw["cats"]
            named = {}
            for cat in ("ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ"):
                names = cats.get(cat, [])
                key = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie"}[cat]
                named[key] = list(dict.fromkeys(names))
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(named.get(cat, []))
            all_votes.append({
                "id": str(vid),
                "session_date": d,
                "session_number": sessions_by_date[d]["number"],
                "topic": raw.get("topic") or "",
                "named_votes": named,
                "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})

    # radni: wszystkie nazwiska pojawiające się w głosowaniach
    name_counts = Counter()
    for v in all_votes:
        for names in v["named_votes"].values():
            name_counts.update(names)
    all_names = list(name_counts.keys())

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": _club_of(name), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0,
                                 "present_sessions": set()}
    for v in all_votes:
        d = v["session_date"]
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors_data.get(name)
                if not c:
                    continue
                c["votes_za"] += cat == "za"
                c["votes_przeciw"] += cat == "przeciw"
                c["votes_wstrzymal"] += cat == "wstrzymal_sie"
                c["present_sessions"].add(d)

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councilors_list = []
    for name in sorted(councilors_data.keys()):
        c = councilors_data[name]
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(c["present_sessions"]) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vv in common if vectors[a][vv] == vectors[b][vv])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(x) for x in all_names)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "sessions": set()})
    for rec in records:
        d = rec["date"]
        for raw in rec["votes"]:
            for cat_en, cat_pl in (("za", "ZA"), ("przeciw", "PRZECIW"), ("wstrzymal_sie", "WSTRZYMUJĘ SIĘ")):
                for name in raw["cats"].get(cat_pl, []):
                    cv[name][cat_en] += 1
                    cv[name]["sessions"].add(d)
    all_sess = {rec["date"] for rec in records}
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie"))
        is_former = NAME_MAP.get(name, (name, False))[1]
        frekw = 100.0 * len(vd["sessions"]) / len(all_sess) if all_sess else 0.0
        profiles.append({
            "name": name, "slug": _slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw, 1), "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"], "votes_wstrzymal": vd["wstrzymal_sie"],
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": is_former, "mid_term": False}}})
    return {"profiles": profiles}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""), "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    global CLUB_ASSIGN
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if args.config and Path(args.config).exists():
        CLUB_ASSIGN = json.loads(Path(args.config).read_text(encoding="utf-8")).get("club_assignments") or {}

    sessions = _harvest(cache_dir)
    print(f"=== Scraper Rada Miasta Mława (bip.mlawa.pl) ===")
    print(f"  Artykuły sesji w kategorii: {len(sessions)}")
    records = []
    for s in sessions:
        art = _html(s["url"], cache_dir)
        hrefs = _voting_pdf_hrefs(art)
        datas = []
        for h in hrefs:
            try:
                datas.append(_fetch(h, cache_dir))
            except Exception as e:
                print(f"    sesja {s['date']}: błąd pobierania {h}: {e}")
        votes = _best_pdf_votes(datas) if datas else []
        if not votes:
            print(f"    sesja {s['date']} ({s['roman']}): 0 głosowań (brak wykazu)")
            continue
        records.append({"date": s["date"], "roman": s["roman"], "votes": votes})
        time.sleep(0.15)
    nvotes = sum(len(r["votes"]) for r in records)
    print(f"  Sesje z głosowaniami: {len(records)}, głosowań: {nvotes}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
