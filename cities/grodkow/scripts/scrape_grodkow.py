#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop grodkow — Tier-2 (roster / "model berliński") scraper.

Brak imiennych glosowań IX kadencji. Miasto dodawane jako Tier-2:
skład rady (roster). Rada Miejska w Grodkowie.

Źródło rosteru: grodkow.pl/328-rada-miejska/6328-sklad-osobowy-rady-miejskiej-w-grodkowie.html
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

KAD = "2024-2029"
NAMES = ["Karolina Rudnicka", "Dariusz Galus", "Agnieszka Gajewska", "Ryszard Jończyk", "Marek Kwiatkowski", "Paweł Birecki", "Paweł Dereń", "Monika Kondysiak", "Damian Macheta", "Ewelina Osadczuk", "Waldemar Kwiatkowski", "Monika Pakos", "Sławomir Mrożek", "Dariusz Gajewski", "Lidia Kanaś", "Dariusz Świerczyński"]


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = list(NAMES)
    print(f"  grodkow roster: {len(names)}")
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [],
        "total_sessions": 0, "total_votes": 0, "total_councilors": len(names),
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
