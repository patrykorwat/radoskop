#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Wolbrom — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań (BIP bip.malopolska.pl/umwolbrom to pusta powłoka
Angular: /api/contexts/umwolbrom 404, brak API sesji; eSesja=wildcard,
AlfaTV/bip.net/bip.gov.pl brak). Strona pokazuje SKŁAD RADY (21 radnych IX
kad. 2024-2029) z komitetów (KWW) — zaczerpnięty z wolbrom.pl/radni-rady-
miejskiej-w-wolbromiu-kadencji i zapisany jako club_assignments w config.json.

Kalendarz sesji via BIP Angular nieosiągalny -> has_speaker_activity:false.
has_voting_data:false, voting_display:faction (roster-mode, kluby z KWW).
"""
import json
import re
import sys
import unicodedata
from pathlib import Path


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def build(city_dir) -> int:
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    assignments = cfg.get("club_assignments", {}) or {}
    clubs = cfg.get("clubs", {}) or {}
    names = sorted(assignments.keys(), key=lambda n: n.split()[-1])
    print(f"  councilors: {len(names)}  clubs: {len(clubs)}")
    if not names:
        print("  [warn] pusty roster — brak club_assignments w config.json")
        return 1

    kadencja = {
        "id": kad, "label": cfg["kadencje"][kad]["label"],
        "clubs": {k: v.get("name", k) for k, v in clubs.items()},
        "sessions": [], "total_sessions": 0, "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": assignments.get(n, ""), "district": None,
                        "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
                        "votes_total": 0, "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{kad}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    scraped_at = __import__("datetime").datetime.now().isoformat()
    profiles = {
        "scraped_at": scraped_at,
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {kad: {"club": assignments.get(n, ""),
                                         "has_voting_data": False, "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({"generated": scraped_at,
                                                "default_kadencja": kad,
                                                "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}]},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(city_dir))
