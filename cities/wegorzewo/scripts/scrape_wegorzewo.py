#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Węgorzewo — imienne głosowania Rady Miejskiej w Węgorzewie.

Źródło: BIP na CMS idcom-jst (bip.wegorzewo.pl). Rada Miejska w Węgorzewie
(IX kadencja 2024-2029, 15 radnych) publikuje per sesję artykuł "Obrady N sesji"
w kategorii Rada Miejska → Sesje Rady Miejskiej → Kadencja 2024-2029 z
załącznikiem "Wyniki głosowań N Sesji Rady Miejskiej" (PDF generowany przez
app.esesja — eSesja PRINT, tekstowy), zawierający głosowania imienne per radny
(ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEOBECNY) + agregaty do walidacji.

Struktura:
  /wiadomosci/17653/kadencja_2024__2029          → lista sesji (pag. /lista/{n})
  /wiadomosci/{cat}/wiadomosc/{id}/...           → artykuł sesji z załącznikami
  https://bip-v1-files.idcom-jst.pl/.../files/{n}_{sesja}_{data}.pdf → PDF

Format PDF (eSesja PRINT, tekstowy, 2-kolumnowa tabela):
  "Głosowanie\n1. Głosowanie w sprawie: {topik}\nTyp głosowania ...\n
   Liczba uprawnionych 15 Głosy za N / Liczba obecnych 15 Głosy przeciw N /
   Liczba nieobecnych 0 Głosy wstrzymujące się N / Głosy nieoddane 0 /
   Kworum ... / Uprawnieni do głosowania /
   Lp Nazwisko i imię Głos  Lp Nazwisko i imię Głos
   1. Kowalski Jan ZA  9. Nowak Anna PRZECIW ..."

Kluby radnych niepublikowane w BIP Węgorzewa → wszystkie NZ (WARN club_quality,
zgodnie z precedensami krosno/głuchołazy/sędziszów itd).

Użycie:
    python scrape_wegorzewo.py --output docs/data.json --profiles docs/profiles.json
                             [--cache-dir .cache]
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
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.wegorzewo.pl"
KAD_CAT = 17653          # /wiadomosci/17653/kadencja_2024__2029
SITE_ID = "47011"        # idcom-jst site id (Węgorzewo)
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Kuratorowany skład rady (z listy obecności sesji; stabilny w całej IX kadencji).
# Źródło (PDF eSesja-print) podaje "Nazwisko Imię"; Radoskop/verify wymaga konwencji
# "Imię Nazwisko" (heurystyka reversed w verify_city), więc zamieniono kolejność.
# Kluby niepublikowane → NZ.
ROSTER = [
    "Wiesław Adamejtis", "Irena Biłat", "Dorota Kozian", "Zbigniew Kozłowski",
    "Marek Lipski", "Wiesław Mickiewicz", "Marcin Mozyro", "Edward Ożga",
    "Mateusz Rodziewicz", "Adam Soczewka", "Iwona Subocz", "Stanisława Szram",
    "Włodzimierz Tymoszczuk", "Tomasz Wierzchowski", "Władysław Żerucha",
]

# Mapowanie nazw surowych z PDF ("Nazwisko Imię") na kanoniczne ("Imię Nazwisko").
_SRC_TO_CANON = {}
for _canon in ROSTER:
    _parts = _canon.split()
    _src = " ".join([_parts[-1]] + _parts[:-1])   # "Nazwisko Imię" (i ewentualne 2. imię zostaje przy nazwisku)
    _SRC_TO_CANON[_src] = _canon
    # także wariant z samym nazwiskiem+1.imieniem (PDF czasem skraca)
    if len(_parts) >= 3:
        _SRC_TO_CANON[f"{_parts[-1]} {_parts[0]}"] = _canon

REQ_DELAY = 0.4
_LAST_REQ = 0.0


def _norm(s: str) -> str:
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = s.replace("\u00b3", "3")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+\s*", "", s).strip()


def _norm_name(n: str) -> str:
    return re.sub(r"\s+", " ", n).strip(" .,;:")


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=50, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data, encoding="utf-8", errors="ignore")
    return data


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji (kadencja -> paginacja -> artykuł -> PDF wyników głosowań)
# ---------------------------------------------------------------------------

def collect_sessions(cache_dir):
    """Zwraca listę dictów: {num, date, article_url, pdf_url, woll}."""
    out = []
    page = 1
    while True:
        u = f"{BIP}/wiadomosci/{KAD_CAT}/lista/{page}/kadencja_2024__2029"
        html = fetch(u, cache_dir)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        content = soup.find("div", class_="Content") or soup
        found = 0
        for a in content.find_all("a", href=True):
            t = a.get_text(strip=True)
            m = re.match(r"Obrady\s+([A-Z]+)\s+sesji.*?dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})", t)
            if m and "/wiadomosc/" in a["href"]:
                num = m.group(1)
                MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
                       'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9,
                       'października': 10, 'listopada': 11, 'grudnia': 12}
                mon_raw = m.group(3).lower()
                mon_norm = "".join(c for c in unicodedata.normalize("NFKD", mon_raw)
                                   if not unicodedata.combining(c))
                mon = MON.get(mon_raw) or MON.get(mon_norm)
                if not mon:
                    continue
                date = f"{m.group(4)}-{mon:02d}-{int(m.group(2)):02d}"
                out.append({"num": num, "date": date, "article_url": a["href"]})
                found += 1
        nxt = content.find("a", string=re.compile("Następna"))
        if not nxt or found == 0:
            break
        page += 1
    # fetch PDF from each article
    for rec in out:
        try:
            ah = fetch(rec["article_url"], cache_dir)
        except Exception:
            rec["pdf_url"] = None
            continue
        pdf = None
        for gm in re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', ah, re.S | re.I):
            href, label = gm.group(1), gm.group(2)
            lt = re.sub(r"<[^>]+>", "", label)
            if re.search(r"wyniki_glosowan|wyniki\s+glosowa", lt + " " + href, re.I) or \
               re.search(r"sesja.*\.pdf", href, re.I):
                pdf = href
                break
        rec["pdf_url"] = pdf
    # dedupe by num
    seen = set()
    dedup = []
    for r in sorted(out, key=lambda x: x["date"]):
        if r["num"] not in seen:
            seen.add(r["num"])
            dedup.append(r)
    return dedup


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF wyników głosowań (eSesja PRINT 2-kolumnowy)
# ---------------------------------------------------------------------------

_VOTE_MAP = {'ZA': 'za', 'PRZECIW': 'przeciw', 'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie',
             'WSTRZYMUJE SIĘ': 'wstrzymal_sie', 'NIEOBECNY': 'nieobecni',
             'NIEOBECNA': 'nieobecni'}


def parse_report_pdf(data):
    """Zwraca (date, [vote dicts]). date = z nagłówka 'N Sesja ... w dniu'? — nie ma
    daty w treści PDF wyników (per-sesja plik), więc date przekazujemy z sesji."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    votes = []
    lines = full.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i].strip()
        if l == "Głosowanie" and i + 1 < len(lines):
            j = i + 1
            topic = ""
            while j < len(lines) and not lines[j].strip().startswith("Typ głosowania"):
                topic += " " + lines[j].strip()
                j += 1
            topic = re.sub(r"^\s*\d+\s+\d+\.\s*", "", topic)
            topic = _norm_name(re.sub(r"\s+", " ", topic))
            agg = {'za': None, 'przeciw': None, 'wstrzymal_sie': None, 'nieoddane': None}
            while j < len(lines) and not lines[j].strip().startswith("Uprawnieni do głosowania"):
                m = re.search(r'Głosy za\s+(\d+)|Głosy przeciw\s+(\d+)|'
                              r'Głosy wstrzymujące się\s+(\d+)|Głosy nieoddane\s+(\d+)',
                              lines[j])
                if m:
                    for k, key in enumerate(['za', 'przeciw', 'wstrzymal_sie', 'nieoddane']):
                        if m.group(k + 1):
                            agg[key] = int(m.group(k + 1))
                j += 1
            named = {'za': [], 'przeciw': [], 'wstrzymal_sie': [], 'nieobecni': []}
            j += 1
            while j < len(lines):
                rl = lines[j].strip()
                if rl.startswith("Wydrukowano") or rl == "Głosowanie" or \
                   (re.match(r'^\d+\s+[IVXLC]+', rl) and "Sesja" in rl):
                    break
                seq = re.findall(r'(\d+)\.\s+([A-ZĄĘŁŃÓŚŹŻ][A-Za-ząęłńóśźż\s\-]+?)'
                                 r'\s+(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIĘ|NIEOBECNY|NIEOBECNA)',
                                 rl)
                if not seq:
                    j += 1
                    continue
                for mm in seq:
                    nm = _norm_name(mm[1])
                    vk = _VOTE_MAP.get(mm[2])
                    nm = _SRC_TO_CANON.get(nm, nm)   # "Nazwisko Imię" -> "Imię Nazwisko"
                    if nm and vk:
                        named[vk].append(nm)
                j += 1
            votes.append({"topic": topic, "agg": agg, "named": named})
            i = j
        else:
            i += 1
    return votes


# ---------------------------------------------------------------------------
# 3. Budowanie outputu (identycznie jak inne miasta Radoskopa)
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name):
    return "NZ"


def _attendance_for(votes):
    """Zwraca zbiór radnych obecnych w sesji (oddali głos za/przeciw/wstrzymal)."""
    att = set()
    for v in votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            att.update(v["named"].get(cat, []))
    return att


def build_output(records, total_councilors):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        for v in rec["votes"]:
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            named = {k: list(n) for k, n in v["named"].items()}
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(named[cat])
            all_votes.append({
                "id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                "topic": v.get("topic", ""), "named_votes": named,
                "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
            "speakers": [],
        })
    all_names = set()
    for vv in all_votes:
        for names in vv["named_votes"].values():
            all_names.update(names)
    # canonical roster — keep only known councilors; any OCR/parse artifacts dropped
    ops = all_names - set(ROSTER)
    if ops:
        print(f"  [warn] nazwiska spoza rostera pominięte: {sorted(ops)}")
    used_names = all_names & set(ROSTER)
    councilors_data = {}
    for name in used_names:
        councilors_data[name] = {
            "name": name, "club": _club_of(name), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    for vv in all_votes:
        for cat, names in vv["named_votes"].items():
            for name in names:
                if name not in councilors_data:
                    continue
                c = councilors_data[name]
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
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for vv in all_votes:
        for cat, names in vv["named_votes"].items():
            if cat != "nieobecni":
                for n in names:
                    councillor_sess[n].add(vv["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })
    vectors = defaultdict(dict)
    for vv in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in vv["named_votes"].get(cat, []):
                vectors[name][vv["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    club_counts = Counter(_club_of(n) for n in used_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("date")
        if not d or d < KAD_START:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                for name in names:
                    if name not in ROSTER:
                        continue
                    key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                        else "wstrzymal_sie" if cat == "wstrzymal_sie" else "nieobecny" if cat == "nieobecni" else "brak"
                    cv[name][key] += 1
                    cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {
                KADENCJA_ID: {
                    "club": _club_of(name), "has_voting_data": True,
                    "has_activity_data": False, "frekwencja": round(frekw, 1),
                    "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                    "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                    "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                    "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                    "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                    "former": False, "mid_term": False,
                }
            }
        })
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
    profiles_path = out_path.parent / "profiles.json"
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("[1/3] Kolekcja sesji...")
    sessions = collect_sessions(cache_dir)
    print(f"  {len(sessions)} sesji IX kadencji")
    for s in sessions:
        print(f"    {s['num']} {s['date']} PDF={'YES' if s.get('pdf_url') else 'NO'}")

    print("[2/3] Parsowanie PDF-ów wyników głosowań...")
    records = []
    n_votes = 0
    n_valid = 0
    for s in sessions:
        if not s.get("pdf_url"):
            continue
        try:
            data = fetch(s["pdf_url"], cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['num']} fetch pdf err: {repr(e)[:60]}")
            continue
        votes = parse_report_pdf(data)
        # walidacja agregatów
        for v in votes:
            n_votes += 1
            a = v["agg"]
            ok = (a["za"] == len(v["named"]["za"]) and
                  a["przeciw"] == len(v["named"]["przeciw"]) and
                  a["wstrzymal_sie"] == len(v["named"]["wstrzymal_sie"]))
            if ok:
                n_valid += 1
            else:
                print(f"  [warn] {s['num']} {v['topic'][:40]} agg={a} n={ {k:len(x) for k,x in v['named'].items()} }")
        records.append({"num": s["num"], "date": s["date"], "votes": votes})
    print(f"  {n_votes} głosowań, {n_valid} zwalidowanych agregatami")

    print("[3/3] Budowanie danych...")
    output = build_output(records, total_councilors=len(ROSTER))
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    print(f"Gotowe! Zapisano do {args.output}")
    print(f"  {output['kadencje'][0]['total_sessions']} sesji, {output['kadencje'][0]['total_votes']} głosowań, "
          f"{output['kadencje'][0]['total_councilors']} radnych")
    print("  Zapisano profiles.json")


if __name__ == "__main__":
    main()
