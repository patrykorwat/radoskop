#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Krynica-Zdrój — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Krynicy-Zdroju na platformie bip.malopolska.pl (Madkom SPA,
encja `umkrynicazdroj`), kategoria Rada -> Sesje -> Protokoły głosowań (menu 310242)
-> 2024-2029 (435991) -> per-rok (2024: 435992, 2025: 453002, 2026: 471837).
36 artykułów = 36 sesji IX kad. (I 2024-05-07 konstytuująca .. XXXV 2026-07-29),
każdy z załącznikiem PDF "Protokół głosowania" (tekstowy, bez OCR) z per-głosowanie:
nagłówek (sesja + "GŁOSOWANIE w sprawie {temat}"), agregaty walidacyjne
(LICZBA UPRAWNIONYCH/OBECNYCH/NIEOBECNYCH, GŁOSY ZA/PRZECIW/WSTRZYMUJĄCE SIĘ/NIEODDANE,
KWORUM) oraz tabela imienna "LP NAZWISKO I IMIĘ GŁOS" z per-rady GŁOSOWANIEM
(ZA / PRZECIW / WSTRZYMUJE SIĘ / NIEODDANY).

API Madkom (bez auth): /api/contexts/umkrynicazdroj, /api/menu/{id}/articles?limit=N,
/api/menu/{id}/submenu, /api/articles/{id}, /api/files/{attachmentId}.

Roster 15 radnych (stalych przez cala kadencje) z tabel imiennych; role (Przewodnicząca,
Wiceprzewodniczący) z kategorii Skład Rady (menu 434952). Kluby radnych NIE publikowane
w BIP (Skład Rady podaje tylko komitety wyborcze) -> wszyscy Niezrzeszeni (NZ),
club_assignments PENDING (WARN club_quality).

Walidacja per-głosowanie: liczba nazwisk w ZA/PRZECIW/WSTRZYMUJE SIĘ/NIEODDANY == agregat.

Użycie:
    python scrape_krynica_zdroj.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache] [--max-sessions N]
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

import pdfplumber
import requests

BASE = "https://bip.malopolska.pl"
API = BASE + "/api"
ENTITY = "umkrynicazdroj"
GLOS_MENU = 310242       # Protokoły głosowań
KAD_MENU = 435991        # 2024 - 2029
YEARS = {2024: 435992, 2025: 453002, 2026: 471837}
SKLAD_MENU = 434952      # Skład Rady 2024-2029 (osobowy)
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
REQ_DELAY = 0.6
_LAST = 0.0

_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
          "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
          "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
          "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,"XXXV":35,"XXXVI":36}


def _rate():
    global _LAST
    now = time.time()
    el = now - _LAST
    if el < REQ_DELAY:
        time.sleep(REQ_DELAY - el)
    _LAST = time.time()


def get_json(url, retries=4):
    _rate()
    for a in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            if a == retries - 1:
                raise
            time.sleep(1.5)
    raise RuntimeError(f"GET {url} failed")


def get_bin(url, retries=4):
    _rate()
    for a in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.content
        except Exception as e:
            if a == retries - 1:
                raise
            time.sleep(1.5)
    raise RuntimeError(f"GET {url} failed")


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    s = str(name or "").lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _norm(s):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    return re.sub(r"[^a-z0-9]", "", str(s).lower().translate(str.maketrans(repl)))


_RE_ROMAN = re.compile(r"([IVX]+)\s*(?:Nadzwyczajna\s*|cz\.\s*\d*\s*)?Sesja", re.I)


def session_num_from_header(header):
    m = _RE_ROMAN.search(header)
    if not m:
        return ""
    return str(_ROMAN.get(m.group(1).upper(), ""))


_MONTHS_WORD = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}
_DATE_RE = re.compile(r"z dnia\s+(\d{1,2})\s*(?:[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})|\s+(\w+)\s+(\d{4}))")


def date_from_header(header):
    m = _DATE_RE.search(header)
    if not m:
        return ""
    d = int(m.group(1))
    if m.group(2) is not None:  # numeric DD.MM.YYYY or DD-MM-YYYY
        mo, y = int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    elif m.group(4) is not None:  # word month "DD miesiąca YYYY r."
        mo = _MONTHS_WORD.get(m.group(4).lower().strip("r. "))
        y = int(m.group(5))
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def collect_articles():
    arts = []
    for y, menu in YEARS.items():
        d = get_json(f"{API}/menu/{menu}/articles?limit=200")
        items = d.get("articles") or d.get("items") or []
        for it in items:
            arts.append({"aid": it.get("id"), "year": y})
    return arts


def article_attachment(art):
    d = get_json(f"{API}/articles/{art['aid']}")
    atts = d.get("attachments") or []
    if not atts:
        return None, art['aid']
    att = atts[0]
    return att, d.get("title", "")


def fetch_pdf(att, cache_dir, art):
    if att is None:
        return None
    aid = art['aid']
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fp = cache_dir / f"{aid}.pdf"
        if not fp.exists():
            fp.write_bytes(get_bin(f"{API}/files/{att['id']}"))
        return fp
    fp = Path(f"/tmp/kr_{aid}.pdf")
    fp.write_bytes(get_bin(f"{API}/files/{att['id']}"))
    return fp


def parse_pdf_text(path):
    blocks = []
    with pdfplumber.open(path) as pdf:
        header = ""
        s = []
        for p in pdf.pages:
            t = (p.extract_text() or "").strip()
            if not t:
                continue
            if not header:
                # capture header (first 3-4 lines: Protokół głosowania/z dnia/...Sesja...)
                lines = t.splitlines()
                header = " ".join(lines[:3])
            s.append(t)
        full = "\n".join(s)
    return header, full


def parse_votes(full, source):
    """Split per-vote blocks and return list of vote dicts."""
    # Markers: header line exactly "GŁOSOWANIE" or "GŁOSOWANIE IMIENNE"
    marker_re = re.compile(r"(?m)^\s*GŁOSOWANIE(?:\s+IMIENNE)?\s*$")
    idxs = [m.start() for m in marker_re.finditer(full)]
    votes = []
    for i, st_i in enumerate(idxs):
        end_i = idxs[i + 1] if i + 1 < len(idxs) else len(full)
        blk = full[st_i:end_i]
        v = parse_block(blk, source)
        if v:
            votes.append(v)
    return votes


_TOKENS = ("ZA", "PRZECIW", "WSTRZYMUJĄCE SIĘ", "WSTRZYMUJĄCY SIĘ", "WSTRZYMUJE SIĘ",
           "WSTRZYMAŁ SIĘ", "WSTRZYMAŁA SIĘ",
           "NIEODDANE", "NIEODDANY", "NIEOBECNY", "NIEOBECNA")


def parse_table(table):
    """Parse the imienne table (after UPRAWNIENI DO GŁOSOWANIA) into list of (name, token).

    Two layouts:
      A) "1 Nazwisko Imię ZA"                       (token at end)
      B) "ZA\\n1 Nazwisko Imię"                       (token on own line before row)
         mixed with "14 Wiewióra Henryk NIEOBECNY"   (token at end on some rows)
    Returns list of [name, token] or [name, None] when unclear.
    """
    out = []
    pending = None
    for raw in table.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "LP NAZWISKO I IMIĘ GŁOS" or "NAZWISKO I IMIĘ" in line:
            continue
        up = line.upper()
        # a lone token line (Layout B prefix)
        if up in _TOKENS:
            pending = line
            continue
        # a name row (starts with number)
        m = re.match(r"^(\d{1,2})\s+(.*)$", line)
        if m:
            rest = m.group(2).strip()
            # token at end?
            done = False
            for t in _TOKENS:
                if rest.upper().endswith(" " + t) or rest.upper() == t:
                    nm = rest[:-len(t)].rstrip()
                    out.append([nm, t])
                    pending = None
                    done = True
                    break
            if not done:
                # token might be on preceding line
                if pending:
                    out.append([rest, pending])
                    pending = None
                else:
                    out.append([rest, None])
            continue
        # unknown line -> drop
        continue
    return out


def parse_block(blk, source):
    lines = blk.splitlines()
    # gather topic until TYP GŁOSOWANIA
    topic_parts = []
    i = 0
    while i < len(lines) and "TYP GŁOSOWANIA" not in lines[i]:
        topic_parts.append(lines[i].strip())
        i += 1
    topic = " ".join(t for t in topic_parts if t).strip()
    body = "\n".join(lines[i:])

    def ag(k):
        m = re.search(rf"{re.escape(k)}\s+(\d+)", body)
        return int(m.group(1)) if m else None

    counts = {
        "za": ag("GŁOSY ZA") or 0,
        "przeciw": ag("GŁOSY PRZECIW") or 0,
        "wstrzymal_sie": ag("GŁOSY WSTRZYMUJĄCE SIĘ") or 0,
        "nieoddane": ag("GŁOSY NIEODDANE") or 0,
        "nieobecni": ag("LICZBA NIEOBECNYCH") or 0,
    }
    if counts["za"] == 0 and counts["przeciw"] == 0 and (counts["wstrzymal_sie"] == 0 and counts["nieoddane"] == 0):
        # could still be a valid all-0? unlikely; require some marker present
        if "TYP GŁOSOWANIA" not in body:
            return None
    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
    tbl_part = body.split("UPRAWNIENI DO GŁOSOWANIA")
    if len(tbl_part) > 1:
        rows = parse_table(tbl_part[1])
        for nm, tok in rows:
            nm2 = re.sub(r"\s+", " ", nm).strip()
            if not nm2 or "NAZWISKO" in nm2:
                continue
            up = (tok or "").upper()
            if up == "ZA":
                named["za"].append(nm2)
            elif up == "PRZECIW":
                named["przeciw"].append(nm2)
            elif up in ("WSTRZYMUJĄCE SIĘ", "WSTRZYMUJĄCY SIĘ", "WSTRZYMUJE SIĘ",
                        "WSTRZYMAŁ SIĘ", "WSTRZYMAŁA SIĘ"):
                named["wstrzymal_sie"].append(nm2)
            elif up in ("NIEODDANE", "NIEODDANY"):
                named["brak_glosu"].append(nm2)
            elif up in ("NIEOBECNY", "NIEOBECNA"):
                named["nieobecni"].append(nm2)
    return {
        "topic": topic, "counts": counts,
        "named_votes": named,
        "source": source,
    }


def canon_roster_from_votes(votes):
    names = set()
    for v in votes:
        for lst in v["named_votes"].values():
            names.update(lst)
    return names


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["num"], "vote_count": 0, "attendees": set()}
        for v in rec["votes"]:
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            nv = v["named_votes"]
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(nv.get(cat, []))
            all_votes.append({
                "id": str(vid), "session_date": d, "session_number": rec["num"],
                "source_url": rec.get("source_url", ""),
                "topic": v.get("topic", ""),
                "named_votes": {k: list(x) for k, x in nv.items()},
                "counts": {k: len(nv.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
        })
    # Roster = union of all named councilors
    all_names = set()
    for vv in all_votes:
        for ns in vv["named_votes"].values():
            all_names.update(ns)
    all_names = sorted(all_names)
    name_index = {n: i for i, n in enumerate(all_names)}
    cdata = {n: {"za":0,"przeciw":0,"wstrzymal":0,"brak":0,"nieobecny":0} for n in all_names}
    for vv in all_votes:
        nv = vv["named_votes"]
        for n in nv.get("za", []): cdata[n]["za"] += 1
        for n in nv.get("przeciw", []): cdata[n]["przeciw"] += 1
        for n in nv.get("wstrzymal_sie", []): cdata[n]["wstrzymal"] += 1
        for n in nv.get("brak_glosu", []): cdata[n]["brak"] += 1
        for n in nv.get("nieobecni", []): cdata[n]["nieobecny"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for vv in all_votes:
        for cat, names in vv["named_votes"].items():
            if cat == "nieobecni":
                continue
            for n in names:
                councillor_sess[n].add(vv["session_date"])
    councilors_list = []
    for name in all_names:
        c = cdata[name]
        present = c["za"]+c["przeciw"]+c["wstrzymal"]+c["brak"]
        aktywnosc = round(present / total_votes * 100, 1) if total_votes else 0.0
        frekwencja = round(len(councillor_sess[name]) / total_sessions * 100, 1) if total_sessions else 0.0
        councilors_list.append({
            "name": name, "slug": make_slug(name), "club": "NZ",
            "frekwencja": frekwencja, "aktywnosc": aktywnosc, "zgodnosc_z_klubem": 0.0,
            "votes_za": c["za"], "votes_przeciw": c["przeciw"], "votes_wstrzymal": c["wstrzymal"],
            "votes_brak": c["brak"], "votes_nieobecny": c["nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })
    votes_out = []
    for vv in all_votes:
        vout = dict(vv)
        nv = vv["named_votes"]
        vout["named_votes"] = {k: [name_index[n] for n in ns if n in name_index] for k, ns in nv.items()}
        vout["counts"] = {k: len(vout["named_votes"][k]) for k in ("za","przeciw","wstrzymal_sie")}
        # drop nieoddane/nieobecni empty arrays keep structure
        votes_out.append(vout)
    # similarity
    vectors = defaultdict(dict)
    for vv in votes_out:
        nv = vv["named_votes"]
        for cat in ("za","przeciw","wstrzymal_sie"):
            for idx in nv.get(cat, []):
                vectors[all_names[idx]][vv["id"]] = cat
    pairs = []
    for a, b in combinations(all_names, 2):
        va, vb = vectors[a], vectors[b]
        common = set(va.keys()) & set(vb.keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if va[vid] == vb[vid])
        pairs.append({"a": a, "b": b, "club_a": "NZ", "club_b": "NZ",
                      "score": round(same/len(common)*100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {"NZ": len(all_names)},
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": votes_out,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
        "councilor_index": all_names, "names_normalized": True,
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records, roster):
    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"nieobecny":0,"brak":0,"votes":[]})
    for rec in records:
        d = rec["date"]
        for v in rec["votes"]:
            for cat, names in v["named_votes"].items():
                key = {"za":"za","przeciw":"przeciw","wstrzymal_sie":"wstrzymal_sie","nieobecni":"nieobecny"}.get(cat, "brak")
                for name in names:
                    cv[name][key] += 1
                    cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(roster):
        vd = cv.get(name)
        if vd is None:
            vd = {"za":0,"przeciw":0,"wstrzymal_sie":0,"nieobecny":0,"brak":0,"votes":[]}
        total = sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","nieobecny","brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = round(100.0 * present_sess / all_sess, 1) if all_sess else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": "NZ", "has_voting_data": True, "has_activity_data": False,
                "frekwencja": frekw, "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"], "votes_wstrzymal": vd["wstrzymal_sie"],
                "votes_brak": vd["brak"], "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False,
            }}
        })
    return {"profiles": profiles}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output["kadencje"]:
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""), "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-sessions", type=int, default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("[1/4] Lista artykułów Protokoły głosowań...")
    arts = collect_articles()
    if args.max_sessions:
        arts = arts[:args.max_sessions]
    print(f"  {len(arts)} artykułów")

    print("[2/4] Pobieranie + parsowanie PDF-ów...")
    records = []
    all_names = set()
    n_votes = 0
    n_valid = 0
    n_fail = 0
    for art in arts:
        att, title = article_attachment(art)
        pdf = fetch_pdf(att, cache_dir, art)
        if pdf is None:
            print(f"  [warn] {art['aid']} brak załącznika")
            n_fail += 1
            continue
        try:
            header, full = parse_pdf_text(pdf)
        except Exception as e:
            print(f"  [warn] {art['aid']} pdf err: {repr(e)[:80]}")
            n_fail += 1
            continue
        votes = parse_votes(full, source=pdf.name)
        num = session_num_from_header(header)
        date = date_from_header(header)
        for v in votes:
            n_votes += 1
            c = v["counts"]
            nv = v["named_votes"]
            ok = (c.get("za") == len(nv.get("za", [])) and
                  c.get("przeciw") == len(nv.get("przeciw", [])) and
                  c.get("wstrzymal_sie") == len(nv.get("wstrzymal_sie", [])))
            if ok:
                n_valid += 1
        if not votes:
            n_fail += 1
        records.append({"num": num, "date": date or "", "source_url": pdf.name, "votes": votes})
        print(f"  sesja {num:>3} {date}  {len(votes)} głosowań")
    print(f"  {n_votes} głosowań, {n_valid} zwalidowanych agregatami, {n_fail} PDF-ów bez głosowań")

    # roster from all named votes
    for rec in records:
        for v in rec["votes"]:
            for ns in v["named_votes"].values():
                all_names.update(ns)
    roster = sorted(all_names)
    print(f"  roster: {len(roster)} radnych")

    print("[3/4] Budowanie danych...")
    output = build_output(records)
    profiles = build_profiles(records, roster)
    save_split(output, args.output, profiles)
    k0 = output["kadencje"][0]
    print(f"[4/4] Gotowe! {k0['total_sessions']} sesji, {k0['total_votes']} głosowań, {k0['total_councilors']} radnych")


if __name__ == "__main__":
    main()
