#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Sandomierz — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: protokoły z sesji RM (bip.um.sandomierz.pl, kategoria
12482) zawierają wyłącznie agregaty ('Wynik głosowania: „za” – 20; „przeciw” – 0;
„wstrzymujących się” – 0'), brak tabel imiennych. sandomierz.esesja.pl = wildcard
(marketing), AlfaTV / bip.net.pl — brak.

Skład: https://bip.um.sandomierz.pl/11764/sklad-rady-miasta-ix-kadencji.html
(tabela: Przewodniczący / Wiceprzewodniczący + lista RADNI "Imię Nazwisko").
Kalendarz: kategoria 12482 Protokoły z sesji — nagłówki 'Protokół Nr N z <ROMAN>
sesji ... w dniu D miesiąca RRRR' (strona główna kategorii = pełna lista IX kad.).
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

BIP = "https://bip.um.sandomierz.pl"
ROSTER_URL = f"{BIP}/11764/sklad-rady-miasta-ix-kadencji.html"
SESSIONS_URL = f"{BIP}/12482/protokoly-z-sesji-rady-miasta.html"
KAD_START = "2024-05-07"
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
          "listopada": 11, "grudnia": 12}
ROMAN_TO_INT = {}
_vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
         (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def roman_to_int(s):
    s = s.upper()
    n = i = 0
    for v, sym in _vals:
        while s[i:i + len(sym)] == sym:
            n += v
            i += len(sym)
    return n if i == len(s) else None


def _get(url, tries=4):
    for att in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=45, context=_CTX) as r:
                b = r.read(4000000)
            head = b[:2500].decode("latin-1", "replace").lower()
            enc = "cp1250" if "charset=cp1250" in head else "utf-8"
            return b.decode(enc, "replace")
        except Exception:
            if att == tries - 1:
                raise
            time.sleep(2 + att * 3)


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


NAME_RE = r"[A-ZŁŚŻŹĆĘÓĄ][a-ząćęłńóśżźŁŚŻŹĆĘÓĄ]+(?:-[A-Zł][a-ząćęłńóśżź]+)?"


def fetch_roster(log):
    t = _get(ROSTER_URL)
    body = re.sub(r"<(script|style).*?</\1>", " ", t, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = body.replace("&nbsp;", " ")
    body = re.sub(r"\s+", " ", body)
    people = {}  # name -> role
    m = re.search(r"Sk\u0142ad osobowy Rady Miasta\s*:(.*?)(?:XML|Drukuj|Metryczka|$)", body)
    seg = m.group(1) if m else body
    # roles: 'Imię Nazwisko – Przewodniczący...', 'Imię Nazwisko - Wiceprzewodniczący'
    for first, last, role in re.findall(
            r"(" + NAME_RE + r")\s+(" + NAME_RE + r")\s*[-–—]\s*(Przewodniczacy|Przewodniczący|Przewodnicząca|Wiceprzewodniczący|Wiceprzewodnicząca)\w*",
            seg):
        people[f"{first} {last}"] = role
    # plain list after RADNI:
    m2 = re.search(r"RADNI:(.*?)(?:XML|Drukuj|Metryczka|Komisje|Oświadczenia|Harmonogram|$)", seg)
    if m2:
        lst = m2.group(1)
        for first, last in re.findall(r"(" + NAME_RE + r")\s+(" + NAME_RE + r")", lst):
            nm = f"{first} {last}"
            people.setdefault(nm, "Radny")
    log(f"roster: {len(people)} osób")
    return people


def fetch_sessions(log):
    seen = {}
    zero_streak = 0
    pages = [SESSIONS_URL] + [f"{SESSIONS_URL}?Page={p}" for p in range(1, 6)]
    for purl in pages:
        try:
            t = _get(purl)
        except Exception:
            break
        before = len(seen)
        for num, roman, dd, mon, yyyy in re.findall(
                r"Protok[oó]\u0142 Nr (\d+) z (\w+)(?: nadzwyczajnej)? [Ss]esji[^w]*w dniu (\d{1,2}) (\w+) (\d{4})", t):
            if mon not in MONTHS:
                continue
            iso = f"{yyyy}-{MONTHS[mon]:02d}-{int(dd):02d}"
            if iso < KAD_START:
                continue
            rn = roman_to_int(roman)
            key = (num, iso)
            if key not in seen:
                seen[key] = {"date": iso, "num": roman, "protocol_nr": num}
        if len(seen) == before:
            zero_streak += 1
            if zero_streak >= 2:
                break
        else:
            zero_streak = 0
    sessions = sorted(seen.values(), key=lambda s: s["date"])
    log(f"sesje IX kad: {len(sessions)}")
    return sessions


def build(city_dir, log):
    out = city_dir / "docs"
    out.mkdir(parents=True, exist_ok=True)
    roster = fetch_roster(log)
    sessions = fetch_sessions(log)
    if not roster:
        log("BRAK ROSTERU — przerywam (nie fabrykuję)")
        return 1

    councilors = sorted(roster)
    profiles = {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": [],
                "total": len(councilors)}
    for nm in councilors:
        role = roster.get(nm, "")
        role = (role.replace("Przewodniczacy", "Przewodniczący"))
        profiles["profiles"].append({
            "name": nm, "slug": _slug(nm), "club": "", "role": role, "photo_url": "",
            "bio": "", "email": "", "social_links": {},
            "voting": None,
            "kadencje": {KAD: {"club": "", "has_voting_data": False, "role": role}},
        })

    sess_rows = [{"date": s["date"], "number": s["num"],
                  "label": f"Sesja {s['num']} ({s['date']})",
                  "vote_count": 0} for s in sessions]
    kad = {
        "id": KAD, "label": KAD_LABEL,
        "sessions": sess_rows, "votes": [],
        "councilor_index": councilors,
        "councilors": [{"name": nm, "slug": _slug(nm), "club": "",
                        "role": roster.get(nm, "")} for nm in councilors],
        "total_councilors": len(councilors),
        "total_votes": 0,
        "similarity_top": [], "similarity_bottom": [],
    }
    (out / f"kadencja-{KAD}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    data = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "city": "Sandomierz",
        "kadencje": [{"id": KAD, "label": KAD_LABEL}],
        "stats": {"sessions": len(sess_rows), "votes": 0, "councilors": len(councilors)},
    }
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    (out / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    log(f"DONE tier2: {len(councilors)} radnych, {len(sess_rows)} sesji")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    args = ap.parse_args()
    sys.exit(build(Path(args.city_dir), lambda *a: print(*a, flush=True)))
