#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Skwierzyna — Tier-2 (roster / "model berliński") scraper.

Imienne rejestry głosowań BIP (kategoria 202 'Imienne wykazy głosowań') to
SKANY eSesja/Rada24-print bez warstwy tekstu — OCR nie daje wiarygodnej
atrybucji per-radny (wielokolumnowy układ nazwisk, liczniki nie reconcilują).
Strona pokazuje SKŁAD RADY (roster, kuratorowany w config.json) + KALENDARZ
SESJI IX kadencji pobierany NA ŻYWO z paginowanego rejestru BIP:
  https://bip.skwierzyna.pl/202/Imienne_wykazy_glosowan/[/N/]
  artykuły: /202/<id>/XX_sesja_Rady_Miejskiej_w_Skwierzynie_DD_MM_YYYY_r/
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

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

HERE = Path(__file__).resolve().parent
BASE = "https://bip.skwierzyna.pl/202"

ROM = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
    "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
    "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
    "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
    "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32,
    "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36, "XXXVII": 37,
    "XXXVIII": 38, "XXXIX": 39, "XL": 40,
}
RE_ART = re.compile(
    r'href="https://bip\.skwierzyna\.pl/202/(\d+)/([a-zA-Z0-9]+)_sesja_Rady_Miejskiej_w_Skwierzynie_(\d{2})_(\d{2})_(\d{4})_r/'
)


def _http(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Radoskop/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return r.read(1200000).decode("utf-8", "replace")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_sessions() -> list[dict]:
    sessions: dict[str, dict] = {}
    empty = 0
    page = 1
    while page <= 12:
        url = (f"{BASE}/Imienne_wykazy_glosowan/" if page == 1
               else f"{BASE}/Imienne_wykazy_glosowan/{page}/")
        try:
            html = _http(url)
        except Exception as e:
            print(f"  [warn] fetch {url}: {e}")
            break
        new = 0
        for m in RE_ART.finditer(html):
            _aid, roman, dd, mm, yy = m.groups()
            date = f"{yy}-{mm}-{dd}"
            if date >= "2024-05-07" and roman.upper() not in sessions:
                sessions[roman.upper()] = {"date": date, "number": ROM.get(roman.upper())}
                new += 1
        if new == 0:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
        page += 1
        time.sleep(0.3)
    return sorted(sessions.values(), key=lambda s: s["date"])


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    assignments = cfg.get("club_assignments", {}) or {}
    clubs = cfg.get("clubs", {}) or {}
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    names = sorted(assignments.keys(), key=lambda n: n.split()[-1])
    sessions = fetch_sessions()
    print(f"  sessions: {len(sessions)} (najnowsza: {sessions[-1]['date'] if sessions else '-'})")

    kadencja = {
        "id": kad,
        "label": cfg["kadencje"][kad]["label"],
        "clubs": {k: v.get("name", k) for k, v in clubs.items()},
        "sessions": [
            {"date": s["date"], "number": s["number"], "vote_count": 0,
             "attendee_count": None, "attendees": [], "speakers": []}
            for s in sessions
        ],
        "total_sessions": len(sessions),
        "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [
            {"name": n, "club": assignments.get(n, ""), "district": None,
             "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
             "votes_total": 0, "rebellion_count": 0, "has_activity_data": False}
            for n in names
        ],
        "votes": [],
        "similarity_top": [],
        "similarity_bottom": [],
    }
    (docs / f"kadencja-{kad}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [
            {"name": n, "slug": _slug(n),
             "kadencje": {kad: {"club": assignments.get(n, ""),
                                "has_voting_data": False,
                                "has_activity_data": False,
                                "former": False, "mid_term": False}}}
            for n in names
        ],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kad,
        "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}],
    }
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = HERE.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(Path(city_dir)))
