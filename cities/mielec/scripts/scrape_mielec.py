#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop mielec — Tier-2 (roster / "model berliński") scraper.

Brak glosowan imiennych: Rada Miejska w Mielcu publikuje na mielec.pl
(wordpress + JS challenge) i mielec.bip.gov.pl (SSDIP) wyłącznie projekty
uchwal + zawiadomienia o sesjach; brak kategorii "wyniki glosowan imiennych".
eSesja = wildcard (korporacyjna strona), AlfaTV brak, bip.net.pl 404.

Dane ( zweryfikowane 2026-09-02, playwright przez challenge mielec.pl):
  * sklad rady IX kadencji: /rada-miejska/sklad-rady-miejskiej/ — 23 radnych
    (zastępstwa śródkańcowe uwzględnione: Wdowiarz, M. Leś, D. Leś, Zając,
    Śpiewak za Przybyłę od 21.08.2026)
  * kalendarz sesji: /sesje-rady-miejskiej/ (archiwum 6 stron) — 27 sesji
    IX kadencji 2024-05-22 … 2026-08-21 (daty z tytułów/URL-i sesji)
"""
import datetime
import json
import re
import unicodedata
from pathlib import Path

KAD = "2024-2029"

# Skład zweryfikowany 2026-09-02 ze strony Rady Miejskiej mielec.pl
NAMES = [
    "Marian Kokoszka", "Jakub Blicharczyk", "Damian Gąsiewski", "Bogdan Bieniek",
    "Grzegorz Celarek", "Agnieszka Cieplińska", "Mirosława Gorazd", "Agnieszka Jastrzębska",
    "Bogusław Kołacz", "Dawid Leś", "Marta Leś", "Adriana Miłoś", "Zdzisław Nowakowski",
    "Andrzej Skowron", "Józef Stala", "Anna Surowiec", "Łukasz Szebla", "Katarzyna Śpiewak",
    "Dorota Trzpis", "Barbara Wdowiarz", "Robert Wójcik", "Jacek Zając", "Marek Zalotyński",
]
ROLES = {
    "Marian Kokoszka": "Przewodniczący Rady Miejskiej",
    "Jakub Blicharczyk": "Wiceprzewodniczący Rady Miejskiej",
    "Damian Gąsiewski": "Wiceprzewodniczący Rady Miejskiej",
}

# Sesje IX kadencji — zweryfikowane 2026-09-02 z archiwum /sesje-rady-miejskiej/
SESSIONS = [
    ("2026-08-21", "XXVIII"), ("2026-06-30", "XXVII"), ("2026-05-27", "XXVI"),
    ("2026-05-11", "XXV"), ("2026-04-23", "XXIV"), ("2026-03-27", "XXIII"),
    ("2026-03-11", "XXII"), ("2026-02-12", "XXI"), ("2025-12-30", "XX"),
    ("2025-11-27", "XIX"), ("2025-10-24", "XVIII"), ("2025-09-26", "XVII"),
    ("2025-09-18", "XVI"), ("2025-08-29", "XV"), ("2025-06-30", "XIV"),
    ("2025-05-29", "XIII"), ("2025-04-29", "XII"), ("2025-03-28", "XI"),
    ("2025-02-14", "X"), ("2024-12-30", "IX"), ("2024-11-29", "VIII"),
    ("2024-10-25", "VII"), ("2024-09-27", "VI"), ("2024-08-23", "V"),
    ("2024-06-28", "IV"), ("2024-05-29", "III"), ("2024-05-22", "II"),
]


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    sessions = [{"date": d, "number": n, "label": f"Sesja {n} Rady Miejskiej w Mielcu ({d})",
                 "vote_count": 0} for d, n in SESSIONS]
    print(f"  mielec roster: {len(NAMES)}, sessions: {len(sessions)}")
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": sessions,
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(NAMES),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in NAMES],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    now = datetime.datetime.now().isoformat()
    profiles = {
        "profiles": [{"name": n, "slug": _slug(n), "role": ROLES.get(n, ""),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False,
                                         "role": ROLES.get(n, "")}}}
                     for n in NAMES],
        "scraped_at": now,
        "total": len(NAMES),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": now, "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(Path(__file__).resolve().parents[1]))
