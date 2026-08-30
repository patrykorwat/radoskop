#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Jastrowie — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań (eSesja PM-B dead-end; BIP WOKISS bez kategorii
głosowań). Dodawane jako Tier-2: skład rady (roster) + kalendarz sesji IX
kadencji z liczbą obecnych.

Źródła (bip3.wokiss.pl/jastrowie, platforma WOKISS):
  - Skład rady: zasoby/files/biuro_rady/sklad-rm-9-kadencja.pdf — 15 radnych
    IX kadencji (Przewodniczący/Wice + członkowie; nazwiska wyciągnięte i
    zweryfikowane 2026-08-30).
  - Kalendarz sesji: zasoby/files/protokoly_sesji/protokol-N.pdf (N=1..32) —
    każdy protokół zawiera linię "Protokół nr <ROMAN>/<rok> sesji ... odbyła
    się dnia <d> <miesiąc> <rok> roku. / W sesji uczestniczyło <N> radnych".

has_voting_data:false, voting_display:faction (roster-mode).
"""
import io, json, re, sys, time
import urllib.request
import ssl
import pdfplumber
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)", "Accept-Language": "pl,en"}

BASE = "https://bip3.wokiss.pl/jastrowie"
ROSTER_URL = f"{BASE}/zasoby/files/biuro_rady/sklad-rm-9-kadencja.pdf"
PROTO_TPL = f"{BASE}/zasoby/files/protokoly_sesji/protokol-{{n}}.pdf"
KAD = "2024-2029"
KAD_START = "2024-05-07"

# 15 radnych IX kadencji (zweryfikowane z sklad-rm-9-kadencja.pdf)
ROSTER = [
    "Aleksandra Ciołek-Kruszyńska", "Paweł Rochowski", "Monika Król",
    "Edyta Biała", "Krzysztof Nagórski", "Magdalena Krause",
    "Anna Drażba-Klekocka", "Edyta Blok", "Renata Stanisławska",
    "Jolanta Łatka", "Michał Kruszyński", "Bogdan Sobczyk",
    "Paweł Soroń", "Agnieszka Kowgan", "Michał Pawłowski",
]

_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'pazdziernika': 10, 'wrzesnia': 9}

_ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8,
          'IX': 9, 'X': 10, 'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
          'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20, 'XXI': 21,
          'XXII': 22, 'XXIII': 23, 'XXIV': 24, 'XXV': 25, 'XXVI': 26, 'XXVII': 27,
          'XXVIII': 28, 'XXIX': 29, 'XXX': 30, 'XXXI': 31, 'XXXII': 32}


def _http(u, binary=False):
    req = urllib.request.Request(u, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def _slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def _parse_protokol(data):
    """'Protokół nr XXXII/2026 sesji ... odbyła się dnia 25 maja 2026 roku. /
    W sesji uczestniczyło 14 radnych ...' -> (roman, date_iso, attendee_count)."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    rm = re.search(r"Protokół nr\s+([IVXLCDM]+)/", text)
    roman = rm.group(1).upper() if rm else ""
    d = None
    for m in re.finditer(r"(\d{1,2})\s+(\w+)\s+(\d{4})\s*roku", text):
        if m.group(2).lower() in _MON:
            d = m
            break
    date = ""
    if d:
        date = f"{d.group(3)}-{_MON[d.group(2).lower()]:02d}-{int(d.group(1)):02d}"
    at = re.search(r"uczestniczyło\s+(\d+)\s+radnych", text)
    att = int(at.group(1)) if at else None
    return roman, date, att


def fetch_sessions():
    sessions = []
    seen = set()
    for n in range(1, 33):
        try:
            data = _http(PROTO_TPL.format(n=n), binary=True)
        except Exception as e:
            print(f"  [warn] protokol-{n}.pdf: {e}")
            continue
        roman, date, att = _parse_protokol(data)
        if not date or date < KAD_START:
            continue
        if date in seen:
            continue
        seen.add(date)
        sessions.append({"date": date, "number": roman, "num": _ROMAN.get(roman, 99),
                         "attendee_count": att})
        time.sleep(0.2)
    sessions.sort(key=lambda s: s["num"])
    return sessions


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    sessions = fetch_sessions()
    names = ROSTER
    print(f"  jastrowie roster: {len(names)}  sessions IX: {len(sessions)}")

    kad = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"],
                      "attendee_count": s["attendee_count"], "attendees": [],
                      "speakers": [], "vote_count": 0} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
