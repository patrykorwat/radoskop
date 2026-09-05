#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop bilgoraj — Tier-2 (roster / "model berliński") scraper.

Zrodla (umbilgoraj.bip.e-zeto.eu, platforma eZeto BIP, serwerowy HTML):
  * sklad rady: Rada IX kadencja -> "Skład Rady" (mnu4/301) — lista "Nazwisko Imię: email"
    + sklad komisji (role Przewodniczący/Wiceprzewodniczący/Członek)
  * sesje: kategoria "Sesje Rady" (mnu4/300, 4 strony) — dokumenty "XX sesja Rady Miasta";
    data sesji z PDF "Porządek obrad" ("Sesja odbędzie się D miesiąca ROKU roku")
Brak imiennych głosów (eSesja wildcard, brak kategorii wyników głosowań, porządki bez wyników)
— has_voting_data:false.
"""
import datetime
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from io import BytesIO
from pathlib import Path

BASE = "https://umbilgoraj.bip.e-zeto.eu"
KAD = "2024-2029"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
          "pazdziernika": 10, "listopada": 11, "grudnia": 12}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30, context=CTX).read().decode("utf-8", "replace")


def fetchb(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=40, context=CTX).read()


def tresc(txt: str) -> str:
    i = txt.find('id="TRESC"')
    j = txt.find("Miejski System Komunikacji", i)
    return txt[i:j if j > i else i + 20000]


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def normalize_name(raw: str) -> str:
    """BIP podaje 'Nazwisko Imię [Imię]' — zwróć 'Imię [Imię] Nazwisko'."""
    toks = raw.split()
    if len(toks) >= 2:
        return " ".join(toks[1:] + [toks[0]])
    return raw


def get_roster() -> list[dict]:
    txt = tresc(fetch(BASE + "/index.php?type=4&name=btX&func=selectsite&value%5B0%5D=mnu4&value%5B1%5D=301"))
    text = re.sub(r"<[^>]+>", "\n", txt)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    roster = []
    seen = set()
    # sekcja 'Radni Rady Miasta Biłgoraja' — wpisy 'Nazwisko Imię : email'
    in_radni = False
    for l in lines:
        if l.startswith("Radni Rady Miasta Biłgoraja"):
            in_radni = True
            continue
        if in_radni:
            if re.search(r"Komisje RADY|numeru okregu|numeru okręgu", l, re.I):
                break
            mm = re.match(r"^([A-ZŁŚŻĆŃÓĄŹ][\wŁŚŻĆŃÓĄŹ-]+(?:\s+[A-ZŁŚŻĆŃÓĄŹ][\wŁŚŻĆŃÓĄŹ-]+){1,3})\s*:?\s*(?:[A-Za-z0-9._@-]+@rm\.bilgoraj\.pl)?\s*$", l)
            if mm:
                raw = mm.group(1).strip()
                name = normalize_name(raw)
                if name not in seen:
                    seen.add(name)
                    roster.append({"name": name, "role": "", "club": ""})
    # (role komisji pomijane — BIP podaje tylko składy komisji, nie zarząd rady)
    return roster


def roman_to_int(s: str) -> int:
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    tot, prev = 0, 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


def get_sessions() -> list[dict]:
    """Kategoria mnu4/300 — dokumenty sesji (4 strony) + daty z PDF 'Porządek obrad'.

    Paginacja eZeto: GET index.php?<urlencoded naziwa_pagera>=N (testowane 2026-09-05).
    """
    import pymupdf
    docs = []
    seen_ids = set()
    pager = ("type=4&name=bt1&func=dispNaviBar&value%5B0%5D=mnu4&value%5B1%5D=300"
             "&param%5Bwartosc%5D%5B0%5D=mnu4&param%5Bwartosc%5D%5B1%5D=300"
             "&param%5Bfunkcja%5D=selectsite&param%5Bsortuj%5D%5Bkierunek%5D=DESC"
             "&param%5Bsortuj%5D%5Bsort1%5D=datad&param%5Bsortuj%5D%5Bsort2%5D=numer"
             "&param%5Bsortuj%5D%5Bsort3%5D=nazwa&param%5B0%5D={p}")
    import urllib.parse
    for page in range(1, 7):
        if page == 1:
            u = BASE + "/index.php?type=4&name=bt20&func=selectsite&value%5B0%5D=mnu4&value%5B1%5D=300"
        else:
            u = BASE + "/index.php?" + urllib.parse.quote(pager.format(p=page - 1), safe="") + f"={page}"
        try:
            t = tresc(fetch(u))
        except Exception as e:
            print(f"  sesje str {page}: ERR {e}")
            break
        found = 0
        for m in re.finditer(r"<h2>([^<]+)</h2>[\s\S]{0,400}?value%255B0%255D%3D(\d+)", t):
            title, did = m.group(1).strip(), m.group(2)
            if did in seen_ids:
                continue
            seen_ids.add(did)
            found += 1
            docs.append((title, did))
        if found == 0:
            break
    print(f"  bilgoraj sesje-dokumenty: {len(docs)}")
    sessions = []
    for title, did in docs:
        nm = re.match(r"^([IVXL]+)\s+sesja", title, re.I)
        roman = nm.group(1).upper() if nm else ""
        url = BASE + "/index.php?type%3D4%26name%3Dbt29%26func%3Dselectsite%26value%255B0%255D%3D" + did
        date = ""
        try:
            dt = tresc(fetch(url))
            links = re.findall(r'href="(/bip/[^"]+)"[^>]*>\s*Porz\wdek obrad', dt)
            for h in links[:1]:
                import urllib.parse
                full = BASE + urllib.parse.quote(h, safe="/")
                pdf = fetchb(full)
                doc = pymupdf.open(stream=pdf, filetype="pdf")
                t0 = "\n".join(p.get_text() for p in list(doc)[:2])
                dm = re.search(r"odb[eę]dzie si[eę]\s+(\d{1,2})\s+(\w+)\s+(\d{4})", t0, re.I)
                if dm and dm.group(2).lower() in MONTHS:
                    date = f"{dm.group(3)}-{MONTHS[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
                break
        except Exception as e:
            print(f"  {title[:40]}: date ERR {e}")
        sessions.append({"date": date, "number": f"{roman} sesja" if roman else title[:40],
                         "label": title[:80], "vote_count": 0})
    # daty publikacji jako fallback porządku sortowania
    real = [s for s in sessions if s["date"]]
    real.sort(key=lambda x: x["date"], reverse=True)
    return real


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    roster = get_roster()
    sessions = get_sessions()
    print(f"  bilgoraj roster: {len(roster)}, sessions: {len(sessions)}")
    if len(roster) < 10:
        raise RuntimeError("bilgoraj: roster za mały — BIP zmienił strukturę?")
    if not sessions:
        raise RuntimeError("bilgoraj: brak sesji z datą")
    names = [r["name"] for r in roster]
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": sessions,
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    now = datetime.datetime.now().isoformat()
    profiles = {
        "profiles": [{"name": r["name"], "slug": _slug(r["name"]), "role": r.get("role", ""),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False,
                                         "role": r.get("role", "")}}}
                     for r in roster],
        "scraped_at": now,
        "total": len(roster),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": now, "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(build(Path(__file__).resolve().parents[1]))
