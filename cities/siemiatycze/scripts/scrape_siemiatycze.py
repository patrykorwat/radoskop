#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Siemiatycze — imienne głosowania Rady Miasta (IX kadencja 2024-2029).

Źródło: BIP bip-umsiemiatycze.podlaskie.eu (serwer-renderowany), kategoria
/rada-miasta/protokoly_z_sesji/ z paginacją rocznikową ?p=<rok>. Per-sesja
artykuł "Protokół nr <R><rr>" linkuje TEKSTOWY PDF protokołu sesji z tabelami
imiennymi po każdym głosowaniu:
  "Lp. Nazwisko i imię głosowanie"
  "1 Bogucki Marek nieobecny"  /  "3 Grygoruk Melania „za”"
  agregat po tabeli: "12 „za”"  (liczba głosów oddanych)
  + narracja "Przewodnicząca Rady zarządziła głosowanie nad podjęciem uchwały
    w sprawie ..." -> temat.
Format stołu: tableta do głosowania (tokeny „za” / „przeciw” / „wstrzymują się
od głosu” / nieobecny/a). Obrót nazwiska: źródło „Nazwisko Imię” -> „Imię Nazwisko”.
Walidacja: suma tokenów z tabeli == agregat po tabeli; inaczej głos odrzucony.

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python3 scrape_siemiatycze.py --city-dir <dir> [--cache-dir dir]
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
import ssl
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber required")

BIP = "https://bip-umsiemiatycze.podlaskie.eu"
CAT = BIP + "/rada-miasta/protokoly_z_sesji/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
REQ_DELAY = 0.4
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10,
          "października": 10, "listopada": 11, "grudnia": 12}


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    req = urllib.request.Request(url, headers=UA)
    data = urllib.request.urlopen(req, timeout=60, context=CTX).read()
    if cache_dir:
        (Path(cache_dir) / (key + ".dat")).write_bytes(data)
    return data


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _roman_val(r):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total, prev = 0, 0
    for ch in reversed(r):
        v = vals.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def _to_roman(n):
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"),
            (4, "IV"), (1, "I")]
    out = ""
    for v, sym in vals:
        while n >= v:
            out += sym
            n -= v
    return out


def _classify(token):
    k = _nk(token)
    if not k:
        return None
    if "nieobecn" in k:
        return "nieobecni"
    if "wstrzym" in k:
        return "wstrzymal_sie"
    if "przeciw" in k:
        return "przeciw"
    if k == "za":
        return "za"
    return None


ROW_RE = re.compile(r"^\s*(\d{1,2})\s+((?:[A-ZŚŁŻŹĆÓĄĘŃ][\wŚŁŻŹĆÓĄĘŃ-]+ ){1,2}[A-ZŚŁŻŹĆÓĄĘŃ][\wŚŁŻŹĆÓĄĘŃ-]+)\s+(\S[^\n]*)$")


def parse_pdf(data):
    """-> dict(date, votes=[{topic, named{za,przeciw,wstrzymal_sie,nieobecni}, agg}])"""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        t = "\n".join((p.extract_text() or "") for p in pdf.pages)
    date = None
    md = re.search(r"w dniu (\d{1,2})\s+([A-Za-ząęłńóśźż]+)\s+(\d{4})", t)
    if md and any(_nk(k) == _nk(md.group(2)) for k in MONTHS):
        mon = [v for k, v in MONTHS.items() if _nk(k) == _nk(md.group(2))][0]
        date = f"{md.group(3)}-{mon:02d}-{int(md.group(1)):02d}"
    lines = t.split("\n")
    votes = []
    i = 0
    while i < len(lines):
        if _nk(lines[i]).startswith("lpnazwisko"):
            # topic: last 'w sprawie ...' / 'nad ...' in preceding 12 lines
            topic = None
            for back in range(i - 1, max(0, i - 14), -1):
                mt = re.search(r"(?:w sprawie|nad|dotyczący[ąch]?|w kwestii)\s+(.+?)[\.\s]*$", lines[back])
                if mt:
                    topic = re.sub(r"\s+", " ", mt.group(1)).strip().rstrip(".")
                    break
            named = defaultdict(list)
            j = i + 1
            while j < len(lines):
                m = ROW_RE.match(lines[j])
                if not m:
                    break
                name, token = m.group(2), m.group(3)
                cat = _classify(token.strip("„”\" "))
                if cat is None:
                    break
                parts = name.split()
                full = " ".join(parts[1:] + parts[:1])  # Nazwisko Imię -> Imię Nazwisko
                named[cat].append(full)
                j += 1
            # aggregate right after table: "12 „za”" etc.
            agg = None
            if j < len(lines):
                ma = re.match(r"^\s*(\d{1,2})\s+\u201e", lines[j])
                if ma:
                    agg = int(ma.group(1))
            votes.append({"topic": topic or "(brak tematu)", "named": dict(named), "agg": agg})
            i = j
        else:
            i += 1
    return {"date": date, "votes": votes}


def validate(v):
    nm = v["named"]
    odd = sum(len(nm.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie"))
    if v["agg"] is not None and odd != v["agg"]:
        return False
    return odd > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default="")
    args = ap.parse_args()
    city = Path(args.city_dir)
    docs = city / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    arts = set()
    for yr in ("2024", "2025", "2026"):
        h = _get(CAT + "?p=" + yr).decode("utf-8", "replace")
        arts |= set(re.findall(r'href="(/rada-miasta/protokoly_z_sesji/protokol[^"]+\.html)"', h))
    print(f"[siemiatycze] artykuly protokolo: {len(arts)}")

    results = {}
    for a in sorted(arts):
        hn = _get(BIP + a).decode("utf-8", "replace")
        mpdf = re.search(r'href="(/resource/[^"]*[Pp]rotok[^"]+)"', hn)
        if not mpdf:
            print(f"[siemiatycze] {a}: brak PDF protokolu")
            continue
        pdfurl = BIP + mpdf.group(1)
        mnum = re.search(r"protokol-nr-([ivxl]+)", a)
        num = _roman_val(mnum.group(1).upper()) if mnum else None
        parsed = parse_pdf(_get(pdfurl, args.cache_dir or None))
        if not parsed["date"] or not parsed["votes"]:
            print(f"[siemiatycze] {a}: BRAK daty/glosowan")
            continue
        okv = sum(1 for v in parsed["votes"] if validate(v))
        print(f"[siemiatycze] ses {num} {parsed['date']}: {len(parsed['votes'])} tablic "
              f"({okv} zwalid.)")
        results[a] = parsed

    all_votes = []
    seen_names = set()
    for a, s in results.items():
        if s["date"] < KAD_START:
            continue
        for v in s["votes"]:
            if not validate(v):
                continue
            nm = {k: v["named"].get(k, []) for k in ("za", "przeciw", "wstrzymal_sie", "nieobecni")}
            for k in nm:
                seen_names.update(nm[k])
            all_votes.append({"date": s["date"], "topic": v["topic"], **nm})
    n_sess = len({v["date"] for v in all_votes})
    print(f"[siemiatycze] sesji IX: {n_sess}, glosowan: {len(all_votes)}, radni: {len(seen_names)}")
    if len(all_votes) < 20:
        raise SystemExit(f"ZA MAŁO głosów ({len(all_votes)}) — przerywam")

    councilors_seen = sorted(seen_names)
    all_votes.sort(key=lambda v: v["date"])
    sessions_data = []
    by_sess = defaultdict(list)
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess[v["date"]].append(v)
    for k, dd in enumerate(sorted(by_sess), 1):
        sessions_data.append({"date": dd, "number": dd,
                              "label": f"Sesja {k} ({dd})",
                              "vote_count": len(by_sess[dd])})

    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": v["date"],
                          "topic": v["topic"], "named_votes": nv,
                          "counts": {"for_": len(v["za"]), "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v["nieobecni"])}})
    total_votes = len(votes_out)
    total_sessions = len(sessions_data)
    cdata = {n: {"name": n, "club": "", "votes_za": 0, "votes_przeciw": 0,
                 "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0}
             for n in councilors_seen}
    csess = defaultdict(set)
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                        ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in cdata:
                    cdata[nm][key] += 1
                    csess[nm].add(v["session_date"])
    councilors_list = []
    for cc in cdata.values():
        present = cc["votes_za"] + cc["votes_przeciw"] + cc["votes_wstrzymal"]
        councilors_list.append({
            "name": cc["name"], "club": "", "district": None,
            "frekwencja": round((len(csess.get(cc["name"], set())) / total_sessions * 100) if total_sessions else 0, 1),
            "aktywnosc": round((present / total_votes * 100) if total_votes else 0, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
            "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
            "votes_nieobecny": cc["votes_nieobecny"],
            "votes_total": present,
            "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    def slugify(nm):
        s = unicodedata.normalize("NFKD", nm.lower())
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.replace("ł", "l")
        return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip().replace(" ", "-")

    profiles = {"profiles": [{"name": cc["name"], "slug": slugify(cc["name"]),
                              "kadencje": {KADENCJA_ID: {
                                  "club": cc["club"], "has_voting_data": True,
                                  "has_activity_data": False,
                                  "frekwencja": cc["frekwencja"], "aktywnosc": cc["aktywnosc"],
                                  "zgodnosc_z_klubem": 0.0,
                                  "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
                                  "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
                                  "votes_nieobecny": cc["votes_nieobecny"],
                                  "votes_total": cc["votes_total"],
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": [], "notes": "", "former": False,
                                  "mid_term": False}}}
                             for cc in councilors_list],
                "total": len(councilors_list)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[siemiatycze] ZAPISANO: {total_sessions} sesji, {total_votes} głosowań, "
          f"{len(councilors_list)} radnych")


if __name__ == "__main__":
    main()
