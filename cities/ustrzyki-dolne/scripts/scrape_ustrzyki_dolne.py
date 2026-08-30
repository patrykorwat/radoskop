#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ustrzyki Dolne — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań (BIP bip.ustrzyki-dolne.pl + portal ustrzyki-dolne.pl
nie publikują tabeli per-radny; tylko lista radnych + kalendarz sesji).
Miasto dodawane jako Tier-2: skład rady (roster) + kalendarz sesji IX kadencji.

Źródła (strona portalowa ustrzyki-dolne.pl, stary custom CMS):
  - Skład rady: bip.ustrzyki-dolne.pl/strona-232-radni.html — 15 radnych
    IX kadencji z pełnym składem (Przewodniczący/Wice/Członkowie).
  - Kalendarz sesji: ustrzyki-dolne.pl/strona-1306-sesje_rady_miejskiej.html
    — per-sesja podstrony "N Sesja Rady Miejskiej w Ustrzykach Dolnych z dnia
    DD.MM.YYYY" (IX kadencja = I..XXXIV, 2024-05-06..2026-07-15).

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json
import re
import sys
import urllib.request
import ssl
from bs4 import BeautifulSoup
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)", "Accept-Language": "pl,en"}

BIP = "https://bip.ustrzyki-dolne.pl"
SITE = "https://ustrzyki-dolne.pl"
ROSTER_URL = f"{BIP}/strona-232-radni.html"
SESSIONS_URL = f"{SITE}/strona-1306-sesje_rady_miejskiej.html"
KAD_START = "2024-05-07"
KAD = "2024-2029"

# 15 radnych IX kadencji (z bip.ustrzyki-dolne.pl/strona-232-radni.html)
ROSTER = [
    "Arkadiusz Lupa", "Renata Wolańska", "Bogdan Kwaśnik", "Bożena Bałkota",
    "Wojciech Chudy", "Julian Czarnecki", "Leszek Dobosz", "Jan Fedczak",
    "Małgorzata Iwanik", "Mariusz Maczyszyn", "Katarzyna Ożóg", "Robert Piotrowicz",
    "Paweł Sykała", "Adam Szary", "Czesław Urban",
]

_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
          "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,
          "XIX":19,"XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,
          "XXVII":27,"XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,
          "XXXIV":34,"XXXV":35,"XXXVI":36,"XXXVII":37,"XXXVIII":38,"XXXIX":39,"XL":40}


def _http(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def fetch_sessions():
    """Sesje IX kadencji (roman I..XXXIV, date >= KAD_START) z landinga sesji."""
    html = _http(SESSIONS_URL)
    soup = BeautifulSoup(html, "lxml")
    sessions = []
    seen = set()
    for a in soup.find_all("a", href=True):
        t = _norm(a.get_text(" ", strip=True))
        # "I Sesja Rady Miejskiej w Ustrzykach Dolnych z dnia 06.05.2024 r."
        m = re.match(r'^([IVXL]+) Sesja', t)
        if not m:
            continue
        rom = m.group(1)
        num = _ROMAN.get(rom)
        if not num or num > 40:
            continue
        dm = re.search(r'z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', t)
        if not dm:
            continue
        day, mon, yr = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        date = f"{yr:04d}-{mon:02d}-{day:02d}"
        if date < KAD_START:
            continue
        # filtr: tylko sesje dalej opisane 'Rady Miejskiej' (nie Uchwały/komisje)
        if "Rady Miejskiej" not in t and "Rady Miejs" not in t:
            continue
        key = date
        if key in seen:
            continue
        seen.add(key)
        sessions.append({"date": date, "number": rom, "roman": rom, "title": t[:90]})
    sessions.sort(key=lambda s: s["date"])
    return sessions


def _slug(name):
    s = name.lower()
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    sessions = fetch_sessions()
    names = ROSTER
    print(f"  ustrzyki-dolne roster: {len(names)}  sessions IX: {len(sessions)}")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["roman"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": __import__("datetime").datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": __import__("datetime").datetime.now().isoformat(),
            "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
