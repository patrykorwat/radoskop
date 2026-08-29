#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Gniezno — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych (skany bez wiarygodnej atrybucji per-radny) —
strona pokazuje SKŁAD RADY (roster) + KALENDARZ SESJI IX kadencji.

Skład (23 radnych) + kluby brane z config.json (club_assignments,
kuratorowane z bip.gniezno.eu/sklad_rady.pdf + kluby_radnych.pdf).
Kalendarz sesji pobierany NA ŻYWO z listy protokołów BIP UM Gniezna
(idcom-jst, /wiadomosci/10012/protokoly_z_sesji_rady_miasta).
"""
import json
import re
import sys
import unicodedata
import urllib.request
import ssl
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _http(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Radoskop/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")

HERE = Path(__file__).resolve().parent

LISTING = "https://bip.gniezno.eu/wiadomosci/10012/protokoly_z_sesji_rady_miasta"
BASE = "https://bip.gniezno.eu/wiadomosci/10012"
MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
         "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
         "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19,
         "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25,
         "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30,
         "XXXI": 31, "XXXII": 32, "XXXIII": 33, "XXXIV": 34}


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_sessions() -> list[dict]:
    sessions = {}
    for year in ("2024", "2025", "2026"):
        idx = 1
        empty = 0
        while idx <= 8:
            url = f"{BASE}/lista/{idx}/{year}"
            try:
                html = _http(url)
            except Exception as e:
                print(f"  [warn] fetch {url}: {e}")
                break
            new = 0
            for m in re.finditer(
                r'<a[^>]+href="(https://bip\.gniezno\.eu/wiadomosci/10012/'
                r'wiadomosc/\d+/[^"]*)"[^>]*>(.*?)</a>', html, re.S):
                href = m.group(1).strip()
                title = re.sub(r"<[^>]+>", " ", m.group(2))
                title = " ".join(title.split())
                if "Protokół" not in title:
                    continue
                rm = re.search(r"Protokół nr\s+([IVXL]+)", title)
                dm = re.search(r"z\s+(\d{1,2})\s+(\w+)\s+(\d{4})", title)
                if not (rm and dm):
                    continue
                num = ROMAN.get(rm.group(1))
                day, monw, yr = int(dm.group(1)), dm.group(2).lower().strip("."), int(dm.group(3))
                mon = MONTHS.get(monw)
                if not mon:
                    continue
                date = f"{yr:04d}-{mon:02d}-{day:02d}"
                if date >= "2024-05-07" and href not in sessions:
                    sessions[href] = {"date": date, "number": num}
                    new += 1
            idx += 1
            if new == 0:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
    return sorted(sessions.values(), key=lambda s: s["date"])


def build(city_dir) -> int:
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assignments = cfg.get("club_assignments", {}) or {}
    clubs = cfg.get("clubs", {}) or {}
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    names = sorted(assignments.keys(), key=lambda n: n.split()[-1])
    sessions = fetch_sessions()
    print(f"  sessions: {len(sessions)}")

    # kadencja
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

    # profiles
    profiles = {
        "scraped_at": __import__("datetime").datetime.now().isoformat(),
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

    # data.json
    data = {
        "generated": __import__("datetime").datetime.now().isoformat(),
        "default_kadencja": kad,
        "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}],
    }
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # config copy (SPA reads has_voting_data etc.)
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = HERE.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(Path(city_dir)))
