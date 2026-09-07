#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Włodawa — scraper głosowań imiennych (BIP bip.lubelskie.pl, raporty Deputy).

BIP = platforma lubelskie.pl (eBIP). Kategoria 'Protokoły z sesji RM IX kadencji'
= menu id=729: lista dokumentów przez POST /index.php?id=729&action=list-ajax
(DataTables, aaData: id_dokumentu, data_utworzenia, tresc='Protokół z XXX sesji
... z dnia 27 lipca 2026 r.'). Strona dokumentu /index.php?id=729&id_dokumentu=N
&akcja=szczegoly&p2=N ma załącznik 'Głosowanie' = TEKSTOWY PDF scalony
(pdfsam_merge / raport_*.pdf): strony 'RAPORT PRZEPROWADZONEGO GŁOSOWANIA'
systemu kongresowego Deputy. Per strona: temat (linia przed 'Temat głosowania:'),
liczniki 'Głosów ZA:/PRZECIW:/WSTRZ:', blok 'Głosy indywidualne:' = trójki
wierszy <imię nazwisko> / <ZA|PRZECIW|WSTRZYMAL SIĘ|NIEOBECNY> / <lp>.
Walidacja: liczby wpisów = liczniki.

Dodane 2026-09-07 (cron ekspansja 500).
"""
import argparse
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "https://umwlodawa.bip.lubelskie.pl"
MENU = "729"  # Protokoły z sesji Rady Miejskiej IX kadencji (2024-2029)
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
KAD = "2024-2029"
IX_START = "2024-05-07"

MONTHS = {m: i for i, m in enumerate(
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września "
    "października listopada grudnia".split(), 1)}

TITLE_RE = re.compile(r"sesji (?:nadzwyczajnej )?Rady Miejskiej we Włodawie z dnia (\d{1,2}) (\w+) (\d{4})", re.I)
ROMAN_RE = re.compile(r"Protokół z\s+([IVXLCDM]+)", re.I)
ZA_RE = re.compile(r"Głosów ZA:\s*\n\s*(\d+)")
PRZ_RE = re.compile(r"Głosów PRZECIW:\s*\n\s*(\d+)")
WSZ_RE = re.compile(r"Głosów WSTRZ:\s*\n\s*(\d+)")
VOTE_TOK = {"ZA": "za", "PRZECIW": "przeciw",
            "WSTRZYMAL SIE": "wstrzymal_sie", "WSTRZYMAM SIE": "wstrzymal_sie",
            "WSTRZYMUJE SIE": "wstrzymal_sie",
            "NIEOBECNY": "nieobecny", "NIEOBECNA": "nieobecny",
            "BRAK GLOSU": "nieobecny"}


def _tok(s: str):
    s = unicodedata.normalize("NFKD", s.upper())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("Ł", "L")
    return VOTE_TOK.get(re.sub(r"\s+", " ", s).strip())
NAME_RE = re.compile(r"^[A-ZŁŚŻŃ][\wŁŚŻćęłńóśźż-]*(?: [A-ZŁŚŻŃ][\wŁŚŻćęłńóśźż-]*){1,3}$")


def _get(url: str, data: bytes = None) -> bytes:
    req = urllib.request.Request(url, headers=UA, data=data)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", s)


def _slug(name: str) -> str:
    return _norm(name.replace("-", "")) or "radny"


def list_docs() -> list[dict]:
    d = json.loads(_get(f"{BASE}/index.php?id={MENU}&action=list-ajax",
                        data=b"draw=1&start=0&length=200&search[value]=").decode("utf-8"))
    out = []
    for rec in d.get("aaData", []):
        title = " ".join(str(rec.get("tresc", "")).split())
        date = str(rec.get("data_utworzenia", ""))[:10]
        m = TITLE_RE.search(title)
        if not m:
            continue
        mon = MONTHS.get(m.group(2).lower())
        if not mon:
            continue
        sdate = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
        if sdate < IX_START:
            continue
        rm = ROMAN_RE.search(title)
        out.append({"did": rec["id_dokumentu"], "title": title,
                    "date": sdate, "roman": rm.group(1).upper() if rm else sdate,
                    "nadzw": "nadzwyczajnej" in title.lower()})
    out.sort(key=lambda r: r["date"])
    return out


def glosowanie_pdf_urls(did: str) -> list[str]:
    page = _get(BASE + f"/index.php?id={MENU}&id_dokumentu={did}&akcja=szczegoly&p2={did}").decode("utf-8", "replace")
    urls = []
    for m in re.finditer(r'href="([^"]+)"[^>]*title="[^"]*(?:Wy|W)świetl: G[łł]osowanie', page):
        u = m.group(1).replace("?download=1", "")
        if u not in urls:
            urls.append(u)
    if not urls:
        # fallback: załącznik o nazwie含 glosowanie/raport/pdfsam
        for m in re.finditer(r'href="(https://[^"]+/upload/pliki/[^"]+(?:glosow|raport|pdfsam)[^"]*\.pdf)[^"]*"', page, re.I):
            u = m.group(1).replace("?download=1", "")
            if u not in urls:
                urls.append(u)
    return urls


def parse_deputy(pdf: bytes):
    """-> list[{topic,date,sesja,za,przeciw,wstrzymal_sie,per:{name:cat}}] lub [] jeśli walidacja zła."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    votes, bad = [], 0
    for p in doc:
        t = p.get_text()
        if "RAPORT PRZEPROWADZONEGO GŁOSOWANIA" not in t:
            continue
        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t.split("RAPORT")[0])
        sm = re.search(r"(XX?I{0,3}|XC|XL[IX]?|I{0,3}X?X?)\s+Sesja", t)
        tm = re.search(r"RAPORT PRZEPROWADZONEGO GŁOSOWANIA\s*\n(.+?)\nTemat głosowania", t, re.S)
        za = ZA_RE.search(t); pr = PRZ_RE.search(t); ws = WSZ_RE.search(t)
        agg = [int(za.group(1)) if za else 0, int(pr.group(1)) if pr else 0, int(ws.group(1)) if ws else 0]
        im = t.split("Głosy indywidualne:", 1)
        if len(im) < 2:
            continue
        body = re.split(r"System kongresowy", im[1])[0]
        lines = [l.strip() for l in body.split("\n") if l.strip()]
        per = {}
        j = 0
        while j < len(lines) - 1:
            nm = lines[j]
            key = _tok(lines[j + 1])
            if NAME_RE.match(nm) and key:
                lp_ok = (j + 2 >= len(lines)) or re.match(r"^\d+$", lines[j + 2])
                if lp_ok:
                    per.setdefault(nm, key)
                    j += 2 if not (j + 2 < len(lines) and re.match(r"^\d+$", lines[j + 2])) else 3
                    continue
            j += 1
        got = [sum(1 for v in per.values() if v == k) for k in ("za", "przeciw", "wstrzymal_sie")]
        if got != agg:
            bad += 1
            print(f"    AGREGAT-MISMATCH topic='{(tm.group(1) if tm else '?')[:40]}' got={got} agg={agg}")
            continue
        topic = " ".join(tm.group(1).split()) if tm else ""
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
        votes.append({"topic": topic[:250], "date": date,
                      "sesja": sm.group(1) if sm else "",
                      "za": agg[0], "przeciw": agg[1], "wstrzymal_sie": agg[2],
                      "per": per})
    doc.close()
    return votes, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    a = ap.parse_args()
    docs = Path(a.city_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    recs = list_docs()
    print(f"[wlodawa] dokumentów IX: {len(recs)}")
    all_votes, roster, sessions = [], [], []
    seen_pdf = set()
    for r in recs:
        try:
            urls = glosowanie_pdf_urls(r["did"])
        except Exception as e:
            print(f"  [warn] {r['title'][:40]}: {e}")
            continue
        if not urls:
            print(f"  [skip] {r['title'][:50]} — brak załącznika Głosowanie")
            continue
        n_v = 0
        for u in urls:
            if u in seen_pdf:
                continue
            seen_pdf.add(u)
            try:
                pdf = _get(u)
                vs, bad = parse_deputy(pdf)
            except Exception as e:
                print(f"    [warn] {u}: {e}")
                continue
            time.sleep(0.3)
            for v in vs:
                per = v["per"]
                nv = {k: [n for n, c in per.items() if c == k]
                      for k in ("za", "przeciw", "wstrzymal_sie")}
                for n in per:
                    if n not in roster:
                        roster.append(n)
                all_votes.append({"date": v["date"] or r["date"], "session_num": r["roman"],
                                  "topic": v["topic"], "za": nv["za"],
                                  "przeciw": nv["przeciw"], "wstrzymal_sie": nv["wstrzymal_sie"],
                                  "nieobecni_glos": [n for n, c in per.items() if c == "nieobecny"]})
                n_v += 1
        if n_v:
            sessions.append({"date": r["date"], "number": r["roman"],
                             "label": f"Sesja {r['roman']}{' nadzw.' if r['nadzw'] else ''} ({r['date']})",
                             "vote_count": n_v})
        print(f"  {r['date']} {r['roman']}: {n_v} glosowan")
    if len(all_votes) < 20:
        raise SystemExit(f"ZA MAŁO głosów ({len(all_votes)}) — przerywam")

    all_votes.sort(key=lambda v: (v["date"], v["topic"]))
    by_sess = {}
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess.setdefault(v["date"], []).append(v)
    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": v["session_num"], "topic": v["topic"],
                          "named_votes": nv,
                          "counts": {"for_": len(v["za"]), "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v["nieobecni_glos"])}})
    total_votes = len(votes_out)
    total_sessions = len(sessions)
    cdata = {n: {"votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                 "votes_brak": 0, "votes_nieobecny": 0} for n in roster}
    csess = {}
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                        ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in cdata:
                    cdata[nm][key] += 1
                    csess.setdefault(nm, set()).add(v["session_date"])
    councilors_list = []
    for nm, cc in cdata.items():
        present = cc["votes_za"] + cc["votes_przeciw"] + cc["votes_wstrzymal"]
        councilors_list.append({
            "name": nm, "club": "", "district": None,
            "frekwencja": round(len(csess.get(nm, set())) / total_sessions * 100, 1) if total_sessions else 0,
            "aktywnosc": round(present / total_votes * 100, 1) if total_votes else 0,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
            "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": 0,
            "votes_nieobecny": cc["votes_nieobecny"],
            "votes_total": present, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    sessions.sort(key=lambda s: s["date"])
    kad = {"id": KAD, "label": "IX kadencja (2024–2029)", "clubs": {},
           "sessions": sessions, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"profiles": [{"name": cc["name"], "slug": _slug(cc["name"]),
                              "kadencje": {KAD: {
                                  "club": "", "has_voting_data": True,
                                  "has_activity_data": False,
                                  "frekwencja": cc["frekwencja"], "aktywnosc": cc["aktywnosc"],
                                  "zgodnosc_z_klubem": 0.0,
                                  "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
                                  "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": 0,
                                  "votes_nieobecny": cc["votes_nieobecny"],
                                  "votes_total": cc["votes_total"],
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": [], "notes": "", "former": False, "mid_term": False}}}
                             for cc in councilors_list],
               "total": len(councilors_list)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": KAD,
        "kadencje": [{"id": KAD, "label": "IX kadencja (2024–2029)"}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[wlodawa] ZAPISANO: {total_sessions} sesji, {total_votes} głosowań, {len(councilors_list)} radnych")


if __name__ == "__main__":
    main()
