#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ząbkowice Śląskie — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań: BIP bip.zabkowiceslaskie.pl publikuje protokoły sesji
jako SKANY PDF (bez warstwy tekstu) i brak kategorii "wyniki głosowań imiennych";
brak eSesja/AlfaTV/Nefeni/posiedzenia.pl. Miasto jako Tier-2:
skład rady (15 radnych IX kad.) + kalendarz sesji (protokoły + porządki obrad).

Źródła (custom CMS BIP, artykuły/załączniki):
  - Skład: /artykul/379/8612/radni-rady-miejskiej-zabkowic-slaskich-ix
    (bloki "RADNY/RADNA RADY MIEJSKIEJ ZĄBKOWIC ŚLĄSKICH" + imię NAZWISKO).
  - Sesje: /artykuly/384/sesje-rady-miejskiej (porządki obrad "na dzień D M ROK")
    + /artykul/388/8767/protokoly-rady-miejskiej (załączniki "Nr N z dnia D M ROK").

has_voting_data:false, voting_display:faction (roster-mode).
"""
import datetime
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

BASE = "https://bip.zabkowiceslaskie.pl"
KAD = "2024-2029"
KAD_START = "2024-05-07"

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}


def _http(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS),
                                timeout=45, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _title_name(raw):
    """'Krzysztof CUPIAŁ' -> 'Krzysztof Cupiał'."""
    parts = raw.split()
    if not parts:
        return ""
    first = parts[0].capitalize()
    rest = " ".join(p.capitalize() for p in parts[1:])
    return f"{first} {rest}".strip()


def _date_from_text(txt):
    m = re.search(r'(\d{1,2})\s+(' + "|".join(MONTHS) + r')\s+(\d{4})',
                  txt.lower())
    if not m:
        return None
    d, mo, y = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def fetch_roster():
    html = _http(f"{BASE}/artykul/379/8612/radni-rady-miejskiej-zabkowic-slaskich-ix")
    plain = re.sub(r"<[^>]+>", "\n", html)
    plain = re.sub(r"&nbsp;?", " ", plain)
    lines = [l.strip() for l in plain.splitlines() if l.strip()]
    names = []
    for i, l in enumerate(lines):
        if re.match(r"^RAD(NY|NA|A) RADY MIEJSKIEJ ZĄBKOWIC ŚLĄSKICH$", l.upper()):
            if i + 1 < len(lines):
                nm = lines[i + 1]
                if re.match(r"^[A-ZŁŚŃŹŻÓĆĘ][A-Za-złśńźżóćęą\-']+( [A-ZŁŚŃŹŻÓĆĘ][A-ZŁŚŃŹŻÓĆĘ\s\-']+)+$", nm):
                    n = _title_name(nm)
                    if n and n not in names:
                        names.append(n)
    return names


def fetch_sessions():
    dates = set()
    # porządki obrad (category listing 384)
    html = _http(f"{BASE}/artykuly/384/sesje-rady-miejskiej")
    for _h, label in re.findall(
            r'href="(https://bip\.zabkowiceslaskie\.pl/artykul/384/\d+/[^"]+)"[^>]*>([^<]{3,110})',
            html):
        d = _date_from_text(label)
        if d:
            dates.add(d)
    # protokoły (załączniki "Nr N z dnia D M ROK")
    html2 = _http(f"{BASE}/artykul/388/8767/protokoly-rady-miejskiej")
    for _i, label in re.findall(r'attachments/download/(\d+)"[^>]*>\s*([^<]{3,90})', html2):
        d = _date_from_text(label)
        if d:
            dates.add(d)
    sessions = sorted(d for d in dates if d >= KAD_START)
    return sessions


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = fetch_roster()
    sessions = fetch_sessions()
    print(f"  zabkowice-slaskie roster: {len(names)}  sessions IX: {len(sessions)}")
    if len(names) < 10 or len(sessions) < 5:
        raise SystemExit("źle sparzony roster/sesje — przerywam")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": d, "number": d, "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []}
                     for d in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None,
                        "frekwencja": None, "aktywnosc": None,
                        "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.datetime.now().isoformat(),
            "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
