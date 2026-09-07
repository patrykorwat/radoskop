#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Mońki — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań online (wykazy w martwym BIP wrotapodlasia = NXDOMAIN;
eSesja monki.esesja.pl = PM-B bez danych). Miasto jako Tier-2: skład rady +
kalendarz sesji IX kadencji.

Źródła (um-monki.pl, WordPress + REST API):
  - Skład Rady: https://um-monki.pl/rada-miejska/ — lista 15 radnych IX kad.
    (Przewodniczący + 2 wice + radni).
  - Kalendarz sesji: https://transmisja.esesja.pl/monkium — archiwum transmisji
    sesji IX kad. (tytuły per sesja z datami; najnowsza 2026-08-27).

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

ROSTER_URL = "https://um-monki.pl/rada-miejska/"
SESSIONS_URL = "https://transmisja.esesja.pl/monkium"
KAD_START = "2024-05-07"
KAD = "2024-2029"

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
           "pazdziernika": 10, "listopada": 11, "grudnia": 12}


def _http(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=45, context=_CTX).read().decode("utf-8", "replace")


def fetch_roster():
    """15 radnych IX kadencji z sekcji 'Radni Rady Miejskiej w Mońkach:'."""
    html = _http(ROSTER_URL)
    txt = re.sub(r"<[^>]+>", "\n", html)
    txt = txt.replace("&#8211;", "-").replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", l).strip() for l in txt.split("\n")]
    start = next((i for i, l in enumerate(lines) if "Radni Rady Miejskiej w Mońkach" in l), None)
    if start is None:
        raise RuntimeError("sekcja 'Radni Rady Miejskiej' nie znaleziona")
    names = []
    for l in lines[start + 1:]:
        m = re.match(r"^\d+\.\s+(.+?)(?:\s+-\s+\S.*)?$", l)
        if m:
            nm = re.sub(r"\s+", " ", m.group(1)).strip()
            # usuń stopnie/tytuły sprzed imienia (mgr, inż.)
            nm = re.sub(r"^(mgr|inż\.)\s+", "", nm, flags=re.I)
            if nm:
                names.append(nm)
        elif names and (not l or l.lower().startswith(("archiwum", "urząd"))):
            break
    if not (10 <= len(names) <= 25):
        raise RuntimeError(f"roster liczy {len(names)} — nieoczekiwanie")
    return names


def fetch_sessions():
    """Sesje IX kadencji z archiwum transmisji (transmisja.esesja.pl/monkium)."""
    html = _http(SESSIONS_URL)
    sessions = []
    for m in re.finditer(r">\s*([^<>]*?[Ss]esja[^<>]*?)\s*<", html):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        dm = re.search(r"z\s*dnia\s*(\d{1,2})\s*([a-ząęłńóśźż]+)\s*(\d{4})", title, re.I)
        if not dm:
            dm = re.search(r"(\d{1,2})\s*([a-ząęłńóśźż]+)\s*(\d{4})", title, re.I)
        if not dm:
            continue
        mon = _MONTHS.get(dm.group(2).lower())
        if not mon:
            continue
        try:
            date = datetime.date(int(dm.group(3)), mon, int(dm.group(1))).isoformat()
        except ValueError:
            continue
        if date < KAD_START:
            continue
        nm = re.search(r"([IVXL]+)\s+[Ss]esja", title)
        sessions.append({"date": date, "number": nm.group(1) if nm else "", "title": title[:80]})
    sessions.sort(key=lambda s: s["date"])
    seen = set(); out = []
    for s in sessions:
        if s["date"] in seen:
            continue
        seen.add(s["date"]); out.append(s)
    return out


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
    print(f"  monki roster: {len(names)}  sessions IX: {len(sessions)}")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.datetime.now().isoformat(),
            "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
