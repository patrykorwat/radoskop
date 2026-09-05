#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Świebodzice — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych i brak publicznych stenogramów:
  - rada.swiebodzice.pl: brak DNS (AlfaTV nie), swiebodzice.esesja.pl = wildcard
    marketing (0 sesji), swiebodzice.bip.net.pl: brak DNS.
  - BIP bip.swiebodzice.pl: "Sesje Rady i projekty uchwał" (kat. 67) publikuje
    wyłącznie ZAŁĄCZNIKI sesji (porządki, zawiadomienia, projekty) — bez protokołów
    i bez wyników głosowań; kategoria "Uchwały Rady Miejskiej" (68) pusta;
    transmisje tylko wideo (hdsystem stream swiebodzsesja).
Skład rady: BIP kategoria "Oświadczenia majątkowe" (kat. 13) -> podfolder
kadencja_2024-2029/radni/ — pliki nazywane <Imie>_<Nazwisko>_<rok>.pdf, jeden
osobno na osobę. Nazwy plików dają pełny roster IX kad. (21 osób; imiona i
nazwiska w rejestrze BIP bez polskich znaków — nie fabrykujemy diakrytyków).
Kalendarz: jedyne pewne datek IX kad. = data pierwszej sesji (2024-06-03,
data wpływu pierwszego oświadczenia radnego, spójna z zaprzysiężeniem IX kad.).
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

BIP = "https://www.bip.swiebodzice.pl"
ROSTER_URL = f"{BIP}/oswiadczenia-majatkowe/13"
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"
FIRST_SESSION = "2024-06-03"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}


def _get(url, tries=4):
    for att in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=45, context=_CTX) as r:
                return r.read(5000000).decode("utf-8", "replace")
        except Exception:
            if att == tries - 1:
                raise
            time.sleep(2 + att * 3)


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def fetch_roster(log):
    t = _get(ROSTER_URL)
    anch = re.findall(
        r'href="(/upload/upload/oswiadczenia_majatkowe/kadencja_2024-2029/radni/[^"]+)"', t)
    groups = OrderedDict()
    for u in anch:
        fn = urllib.parse.unquote(u.split("/")[-1])
        toks = [x for x in re.split(r"[_\-. ]", fn) if x and not re.match(r"^r?\d{3,4}", x)]
        words = tuple(sorted(w.lower() for w in toks
                             if re.match(r"^[A-ZŁŚŻŹĆĘÓĄ][a-ząćęłńóśżź]+$", w)))
        if len(words) < 2:
            continue
        groups.setdefault(words, []).append(fn)
    people = []
    for words, files in groups.items():
        # latest declaration file (biggest year) uses the 'Imie_Nazwisko' order
        cand = max(files, key=lambda f: int((re.findall(r"(\d{4})", f) or [0])[-1]))
        toks = [x for x in re.split(r"[_\-. ]", cand) if x and not re.match(r"^r?\d{3,4}", x)]
        w2 = [w for w in toks if re.match(r"^[A-ZŁŚŻŹĆĘÓĄ][a-ząćęłńóśżź]+$", w)]
        if len(w2) >= 2:
            people.append(f"{w2[0]} {w2[1]}")
    people = sorted(set(people))
    log(f"roster: {len(people)} osób")
    return people


def build(city_dir, log):
    out = city_dir / "docs"
    out.mkdir(parents=True, exist_ok=True)
    roster = fetch_roster(log)
    if len(roster) < 15:
        log("BRAK PEŁNEGO ROSTERU — przerywam (nie fabrykuję)")
        return 1

    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "profiles": [], "total": len(roster)}
    for nm in roster:
        profiles["profiles"].append({
            "name": nm, "slug": _slug(nm), "club": "", "role": "Radny", "photo_url": "",
            "bio": "", "email": "", "social_links": {},
            "voting": None,
            "kadencje": {KAD: {"club": "", "has_voting_data": False, "role": "Radny"}},
        })

    sessions = [{"date": FIRST_SESSION, "number": "I",
                 "label": f"I Sesja Rady Miejskiej ({FIRST_SESSION})", "vote_count": 0}]
    kad = {
        "id": KAD, "label": KAD_LABEL,
        "sessions": sessions, "votes": [],
        "councilor_index": roster,
        "councilors": [{"name": nm, "slug": _slug(nm), "club": "", "role": "Radny"}
                       for nm in roster],
        "total_councilors": len(roster),
        "total_votes": 0,
        "similarity_top": [], "similarity_bottom": [],
    }
    (out / f"kadencja-{KAD}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    data = {
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "city": "Świebodzice",
        "kadencje": [{"id": KAD, "label": KAD_LABEL}],
        "stats": {"sessions": len(sessions), "votes": 0, "councilors": len(roster)},
    }
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    (out / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    log(f"DONE tier2: {len(roster)} radnych, {len(sessions)} sesji")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    args = ap.parse_args()
    sys.exit(build(Path(args.city_dir), lambda *a: print(*a, flush=True)))
