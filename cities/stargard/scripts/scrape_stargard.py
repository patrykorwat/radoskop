#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Stargard — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych (protokoły sesji w BIP narracyjne, bez tabel ZA/PRZECIW
per radny) — strona pokazuje SKŁAD RADY (roster) + KALENDARZ SESJI IX kadencji.

Skład (23 radnych) + kluby z config.json (club_assignments, kuratorowane z
stargard.eu/dla-mieszkanca/samorzad/sklad-rady-miejskiej + BIP
7973/dokument/62877 Kluby Radnych IX kadencji PDF).
Kalendarz sesji pobierany NA ŻYWO z listy "Sesje Rady Miejskiej" BIP
(bip.stargard.eu kat. 7876, paginacja /wszystkie/strona/N).
"""
import json
import re
import sys
import time
import unicodedata
import urllib.request
import ssl
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BASE = "https://bip.stargard.eu"
LAST_UPDATE = "https://bip.stargard.eu/7876/wszystkie/strona/9"
LAST_CREATE = "https://bip.stargard.eu/7876/wszystkie/strona/10"
LAST_KAD9 = "2024-05-07"


def _http(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Radoskop/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    "styczen": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10,
    "listopad": 11, "grudzień": 12,
}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
         "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
         "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19,
         "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25,
         "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30,
         "XXXI": 31, "XXXII": 32, "XXXIII": 33}
_KAD8 = {"I", "II", "III", "IV"}  # Sesje VIII kad. numerowane od I w 2024 (maj-czerwiec)


def _discover_last_pages():
    """Binary-search-free: find first page containing 'VIII kadencja' marker.
    Simpler: walk from page 1 until a page yields sessions older than
    LAST_KAD9 or has no documents; cap at 15 pages."""
    return 15


def fetch_sessions() -> list[dict]:
    sessions = {}
    max_pages = 15
    for page in range(1, max_pages + 1):
        url = f"{BASE}/7876/wszystkie" + ("" if page == 1 else f"/strona/{page}")
        try:
            html = _http(url)
        except Exception as e:
            print(f"  [warn] fetch {url}: {e}")
            break
        new = 0
        for m in re.finditer(
                r'href="7876/dokument/(\d+)"[^>]*>(.*?)</a>', html, re.S):
            doc_id, raw = m.group(1), m.group(2)
            title = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
            dm = re.search(r"w dniu[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})", title)
            rm = re.match(r"([IVXL]+)\s+(?:Nadzwyczajna\s+)?sesja", title, re.I)
            if not dm:
                continue
            date = f"{int(dm.group(3)):04d}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
            if date < LAST_KAD9:
                continue
            num = ROMAN.get(rm.group(1).upper()) if rm else None
            if date not in sessions:
                sessions[date] = {"date": date, "number": num or date}
                new += 1
        if new == 0 and page >= 12:
            break
        time.sleep(0.4)
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

    data = {
        "generated": __import__("datetime").datetime.now().isoformat(),
        "default_kadencja": kad,
        "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}],
    }
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    HERE = Path(__file__).resolve().parent
    city_dir = HERE.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(Path(city_dir)))
