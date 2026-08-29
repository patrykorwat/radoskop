#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Środa Śląska — imienne głosowania Rady Miejskiej w Środzie Śląskiej.

Źródło: e-BIP bip.srodaslaska.pl (platforma e-bip "index.php?id=N").
Rada Miejska w Środzie Śląskiej (IX kadencja 2024-2029, 15 radnych) publikuje w
kategorii Rada Miejska -> Imienne wykazy głosowań radnych (id=176) per sesję
dokument "wykazy głosowań z N Sesji Rady Miejskiej z dnia ..." — PDF generowany
przez app.esesja.pl (format eSesja-TEXT, tekstowy, bez OCR) z imiennymi
głosowaniami per radny (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI)
+ agregatami do walidacji.

Dostęp do listy dokumentów: index.php?id=176&akcja=pobierz_dokumenty_ajax
  (DataTable JSON: aaData[row], row[9]=id dokumentu, row[2]=tytuł, row[5]=data)
Szczegóły dokumentu:     index.php?id=176&p1=szczegoly&p2={id}  -> upload/pliki/*.pdf
Parsowanie:              lib_voting_pdf_table.extract_pdf_text() + parse_voting_text()
  (sezony 2024 mają "Głosowano w sprawie wniosku..." bez dwukropka; normalizujemy
   do "w sprawie: wniosku", by pasował do split-ów parsera)

Kluby radnych niepublikowane w BIP Środy Śląskiej -> wszystkie NZ (WARN club_quality).

Użycie:
    python scrape_sroda_slaska.py --output docs/data.json --profiles docs/profiles.json
                             [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests

# pdfplumber.PDF.page_count wymaga popplera (pdfinfo); fallback przez pypdf,
# by skraper działał też bez poppler-utils (NAS i tak ma poppler).
try:
    import pdfplumber
    from pypdf import PdfReader
    _orig_pc = pdfplumber.PDF.page_count.fget

    def _safe_page_count(self):
        try:
            return _orig_pc(self)
        except Exception:
            try:
                return len(PdfReader(self.stream or self.path).pages)
            except Exception:
                return len(self.pages)
    pdfplumber.PDF.page_count = property(_safe_page_count)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
from lib_voting_pdf_table import extract_pdf_text, parse_voting_text  # noqa: E402

BIP = "https://bip.srodaslaska.pl"
VOTES_CAT = 176
KAD_START = "2024-05-06"      # sesja konstytuująca I (2024-05-06) wchodzi w IX kadencję
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
H = {"User-Agent": "Mozilla/5.0 (Radoskop scraper)", "X-Requested-With": "XMLHttpRequest"}
REQ_DELAY = 0.25

_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
    "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17,
    "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24,
    "XXV": 25, "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30,
    "XXXI": 31, "XXXII": 32, "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36,
    "XXXVII": 37, "XXXVIII": 38, "XXXIX": 39, "XL": 40,
}

# Kuratorowany skład Rady Miejskiej (IX kadencja) — 15 radnych widocznych w
# imiennych wykazach głosowań (np. sesja X: "ZA (15)").
ROSTER = [
    "Andrzej Bielski", "Arkadiusz Hibner", "Agnieszka Kogutek", "Zdzisław Kruszelnicki",
    "Beata Kuriata", "Bożena Lenartowicz", "Dariusz Maciejewski", "Leszek Maciejewski",
    "Grażyna Ostrówka", "Paweł Rosenbeiger", "Stanisław Sendyka", "Mirosław Skóra",
    "Aneta Słoniowska", "Zbigniew Sozański", "Sabina Wereszczyńska",
    "Anna Wojtasińska-Żygadło",
]
ROSTER_SET = set(ROSTER)


def _rate():
    time.sleep(REQ_DELAY)


def fetch(url, binary=False):
    try:
        r = requests.get(url, headers=H, timeout=40)
        r.raise_for_status()
        return r.content if binary else r.text
    except Exception as e:
        raise RuntimeError(f"fetch {url}: {e!r}")


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def _club_of(name):
    return "NZ"


def _normalize_name(n):
    n = re.sub(r"-\s+", "-", n)      # line-wrap "Anna Wojtasińska- Żygadło" -> "Anna Wojtasińska-Żygadło"
    return " ".join(n.split())


_MONTHS_PL = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "wrze?nia": "09", "pa?dziernika": "10", "listopada": "11", "grudnia": "12",
}

def session_number_from_title(title):
    m = re.search(r"z\s+(cd\.\s*)?([IVX]+)\s+Sesji", title)
    if not m:
        return ""
    num = _ROMAN.get(m.group(2))
    return str(num) if num else m.group(2)


def session_date_from_title(title):
    """Session date from the doc title: 'z dnia 26 sierpnia 2026 roku'."""
    m = re.search(r"z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})", title)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        monkey = mon.replace("rze?nia", "rzęśnia").replace("pa?dziernika", "października")
        mm = _MONTHS_PL.get(monkey)
        if mm and 1 <= int(day) <= 31:
            return f"{year}-{mm}-{int(day):02d}"
    return ""


def collect_docs():
    """Lista wykazów głosowań (IX kad.) z kategorii 176."""
    url = f"{BIP}/index.php?id={VOTES_CAT}&akcja=pobierz_dokumenty_ajax"
    d = json.loads(fetch(url))
    rows = d.get("aaData", [])
    docs = []
    for row in rows:
        title = (row[2] or "").strip()
        date = (row[5] or "").strip()
        docid = row[9]
        if not title.strip(" \t\r\n") or "Sesji Rady Miejskiej" not in title:
            continue
        if date < KAD_START:
            continue
        docs.append({"id": docid, "date": date, "title": title})
    return docs


def pdf_url_for(docid):
    _rate()
    url = f"{BIP}/index.php?id={VOTES_CAT}&p1=szczegoly&p2={docid}"
    txt = fetch(url)
    m = re.findall(r"(upload/pliki/[^\"'<>\s]+)", txt)
    pdfs = sorted(set(p for p in m if ".pdf" in p.lower() and "/images/" not in p))
    return (BIP + "/" + pdfs[0]) if pdfs else None


def parse_with_cache(pdf_url, docid, cache_dir):
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pf = cache_dir / f"{docid}.pdf"
        if not pf.exists():
            pf.write_bytes(fetch(pdf_url, binary=True))
    else:
        pf = Path(f"/tmp/sroda_{docid}.pdf")
        pf.write_bytes(fetch(pdf_url, binary=True))
    full_text, first_page = extract_pdf_text(pf)
    # Sesje 2024: "Głosowano w sprawie wniosku/..." bez dwukropka -> dodaj ":"
    full_text = re.sub(
        r"G[łl]osowano\s+(?:wniosek\s+)?w sprawie(?=\s+[^:])",
        lambda m: m.group(0) + ":",
        full_text,
    )
    return parse_voting_text(full_text, first_page, source_name=pf.name)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set()}
        for v in rec["votes"]:
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            named = {k: [_normalize_name(n) for n in v["named_votes"].get(k, [])]
                     for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(named[cat])
            all_votes.append({
                "id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                "source_url": rec.get("source_url", ""),
                "topic": v.get("topic", ""), "named_votes": named,
                "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
        })
    all_names = set()
    for vv in all_votes:
        for names in vv["named_votes"].values():
            all_names.update(names)
    # filter to roster + drop artifacts; keep only known councilors
    unused = all_names - ROSTER_SET
    for u in sorted(unused):
        print(f"  [warn] nazwisko spoza rostera pominięte: {u!r}")
    all_names = sorted(all_names & ROSTER_SET)
    name_index = {n: i for i, n in enumerate(all_names)}
    cdata = {}
    for name in all_names:
        cdata[name] = {"za": 0, "przeciw": 0, "wstrzymal": 0, "brak": 0, "nieobecny": 0}
    for vv in all_votes:
        nv = vv["named_votes"]
        for n in nv.get("za", []):
            if n in cdata: cdata[n]["za"] += 1
        for n in nv.get("przeciw", []):
            if n in cdata: cdata[n]["przeciw"] += 1
        for n in nv.get("wstrzymal_sie", []):
            if n in cdata: cdata[n]["wstrzymal"] += 1
        for n in nv.get("brak_glosu", []):
            if n in cdata: cdata[n]["brak"] += 1
        for n in nv.get("nieobecni", []):
            if n in cdata: cdata[n]["nieobecny"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for vv in all_votes:
        for cat, names in vv["named_votes"].items():
            if cat != "nieobecni":
                for n in names:
                    if n in councillor_sess:
                        councillor_sess[n].add(vv["session_date"])
    councilors_list = []
    for name in all_names:
        c = cdata[name]
        present = c["za"] + c["przeciw"] + c["wstrzymal"] + c["brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess[name]) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": name, "slug": make_slug(name), "club": _club_of(name),
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["za"], "votes_przeciw": c["przeciw"], "votes_wstrzymal": c["wstrzymal"],
            "votes_brak": c["brak"], "votes_nieobecny": c["nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })
    councilor_index = all_names
    votes_out = []
    for vv in all_votes:
        vout = dict(vv)
        nv = vv["named_votes"]
        vout["named_votes"] = {k: [name_index[n] for n in ns if n in name_index]
                               for k, ns in nv.items()}
        vout["counts"] = {k: len(vout["named_votes"][k]) for k in ("za", "przeciw", "wstrzymal_sie")}
        votes_out.append(vout)
    vectors = defaultdict(dict)
    for vv in votes_out:
        nv = vv["named_votes"]
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for idx in nv.get(cat, []):
                vectors[councilor_index[idx]][vv["id"]] = cat
    pairs = []
    for a, b in combinations(all_names, 2):
        va, vb = vectors[a], vectors[b]
        common = set(va.keys()) & set(vb.keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if va[vid] == vb[vid])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    club_counts = Counter(_club_of(n) for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": votes_out,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
        "councilor_index": councilor_index, "names_normalized": True,
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecny": 0,
                              "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("date")
        if not d or d < KAD_START:
            continue
        for v in rec["votes"]:
            for cat, names in v["named_votes"].items():
                for name in (_normalize_name(n) for n in names):
                    if name not in ROSTER_SET:
                        continue
                    key = {"za": "za", "przeciw": "przeciw", "wstrzymal_sie": "wstrzymal_sie",
                           "nieobecni": "nieobecny"}.get(cat, "brak")
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
                    "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                    "frekwencja": round(frekw, 1), "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
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
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("[1/3] Lista wykazów głosowań (kategoria 176)...")
    docs = collect_docs()
    print(f"  {len(docs)} sesji IX kadencji")
    for d in docs:
        num = session_number_from_title(d["title"])
        print(f"    sesja {num:>3} {d['date']}  {d['title'][:60]}")

    print("[2/3] Pobieranie i parsowanie PDF-ów...")
    records = []
    n_votes = 0
    n_valid = 0
    n_fail = 0
    for d in docs:
        pdf_url = pdf_url_for(d["id"])
        if not pdf_url:
            print(f"  [warn] brak PDF dla {d['id']} {d['date']}")
            n_fail += 1
            continue
        try:
            res = parse_with_cache(pdf_url, d["id"], cache_dir)
        except Exception as e:
            print(f"  [warn] {d['date']} parse err: {repr(e)[:90]}")
            n_fail += 1
            continue
        num = session_number_from_title(d["title"])
        title_date = session_date_from_title(d["title"])
        # Wykorzystaj datę sesji z tytułu (miarodajna) zamiast daty z PDF (błędna/collidująca);
        # fallback na datę dokumentu.
        rec = {"num": num, "date": title_date or d["date"], "source_url": pdf_url,
               "votes": res.get("votes", [])}
        for v in rec["votes"]:
            n_votes += 1
            c = v.get("counts", {})
            nv = v.get("named_votes", {})
            ok = (c.get("za") == len(nv.get("za", [])) and
                  c.get("przeciw") == len(nv.get("przeciw", [])) and
                  c.get("wstrzymal_sie") == len(nv.get("wstrzymal_sie", [])))
            if ok:
                n_valid += 1
        records.append(rec)
    print(f"  {n_votes} głosowań, {n_valid} zwalidowanych agregatami, {n_fail} PDF-ów błędnych")

    print("[3/3] Budowanie danych...")
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    k0 = output["kadencje"][0]
    print(f"Gotowe! {k0['total_sessions']} sesji, {k0['total_votes']} głosowań, "
          f"{k0['total_councilors']} radnych")


if __name__ == "__main__":
    main()
