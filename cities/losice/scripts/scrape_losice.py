#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Łosice — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: BIP gminalosice.pl publikuje protokoły sesji jako
PDF-y tekstowe z WYŁĄCZNIE agregatami per głosowanie ("13 głosów za, 0
przeciw, 0 wstrzymujących"); "wykaz imienny głosujących" jest osobnym
załącznikiem nr 3..N do protokołu, NIGDOSTĘPNYM online (brak kategorii
"Wyniki głosowań"). Skład rady (15 radnych, IX kadencja) + kalendarz sesji
(z listy "Zaproszenia na sesje" — tytuły "Informacja o XX sesji - RRRR-MM-DD").

has_voting_data:false — tylko skład + kalendarz sesji.
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BIP = "https://bip.gminalosice.pl"
ROSTER_URL = f"{BIP}/rada_miasta_i_gminy_losice/ix-kadencja-2024-2029/rada-miasta-i-gminy.html"
SESJE_URL = f"{BIP}/rada_miasta_i_gminy_losice/zaproszenia_na_sesje/"
KAD_START = "2024-05-07"

ROMAN = re.compile(r"\b(XXX|XXIX|XXVIII|XXVII|XXVI|XXV|XXIV|XXIII|XXII|XXI|XX|"
                   r"XIX|XVIII|XVII|XVI|XV|XIV|XIII|XII|XI|X|IX|VIII|VII|VI|"
                   r"V|IV|III|II|I)\b")


def _http(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Radoskop/1.0"})
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster() -> list[dict]:
    html = _http(ROSTER_URL)
    i = html.find("Skład Rady Miasta i Gminy Łosice")
    j = html.find("</ol>", i)
    if i < 0 or j < 0:
        raise RuntimeError("Nie znaleziono listy składu rady (<ol>)")
    seg = html[i:j]
    names = []
    for m in re.finditer(r"<li>(.*?)</li>", seg, re.S):
        raw = re.sub(r"<[^>]+>", " ", m.group(1))
        raw = raw.replace("&nbsp;", " ").replace("–", "-").replace("—", "-")
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            continue
        role = ""
        rm = re.search(r"-\s*(Przewodnicząca Rady|Zastępca Przewodniczącej Rady)\s*$", raw)
        if rm:
            role = rm.group(1)
            raw = raw[:rm.start()].strip()
        nm = " ".join(raw.split())
        if re.match(r"^[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż]+( [\wąćęłńóśźż]+){1,3}$", nm):
            names.append({"name": nm, "role": role})
    if not (13 <= len(names) <= 25):
        raise RuntimeError(f"Suspect roster size: {len(names)}")
    return names


def fetch_sessions() -> list[dict]:
    sessions: dict[str, dict] = {}
    page = 1
    while page <= 10:
        url = SESJE_URL if page == 1 else f"{SESJE_URL}zaproszenia-na-sesje.html?page={page}"
        try:
            html = _http(url)
        except Exception as e:
            print(f"  [warn] {url}: {e}")
            break
        new = 0
        for m in re.finditer(r"Informacja o (?:[a-zA-Zżółćąęńśź]+ )*\(?([IVXL]+)\)? sesji - (\d{4}-\d{2}-\d{2})", html):
            roman, date = m.group(1), m.group(2)
            if date >= KAD_START and date not in sessions:
                sessions[date] = {"date": date, "number": roman}
                new += 1
        if new == 0:
            break
        page += 1
    return sorted(sessions.values(), key=lambda s: s["date"])


def build(city_dir: Path) -> int:
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster = fetch_roster()
    sessions = fetch_sessions()
    print(f"  roster: {len(roster)}  sessions: {len(sessions)}")
    if not sessions or sessions[-1]["date"] < "2026-01-01":
        print("  [warn] kalendarz sesji wygląda na nieświeży")

    names = sorted((r["name"] for r in roster))
    kadencja = {
        "id": kad,
        "label": cfg["kadencje"][kad]["label"],
        "clubs": {},
        "sessions": [
            {"date": s["date"], "number": s["number"], "vote_count": 0,
             "attendee_count": None, "attendees": [], "speakers": []}
            for s in sessions
        ],
        "total_sessions": len(sessions),
        "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [
            {"name": n, "club": "", "district": None,
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

    roles = {r["name"]: r["role"] for r in roster if r["role"]}
    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [
            {"name": n, "slug": _slug(n),
             "kadencje": {kad: {"club": "", "role": roles.get(n, ""),
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
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(city_dir))
