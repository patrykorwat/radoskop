#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ozimek — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań (BIP PRO3W/eBOI; listy protokołów JS-rendered,
nieosiągalne przez HTTP). Skład rady 15 radnych IX kadencji zweryfikowany z
załącznika ozimek.pl/static/img/k01/RM 2024/Komisje IX kadencji.pdf
(skład komisji = skład Rady Miejskiej).

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json, re, sys
from datetime import datetime
from pathlib import Path

KAD = "2024-2029"

# 15 radnych IX kadencji (zweryfikowane z Komisje IX kadencji.pdf)
ROSTER = [
    "Grzegorz Filipek", "Agnieszka Dudek", "Rafał Jurczyński",
    "Marcin Kowalewski", "Ireneusz Kołodziejczyk", "Marcin Golomb",
    "Przemysław Bylak", "Adam Kuboń", "Marek Elis", "Zygmunt Olbryt",
    "Michał Libera", "Barbara Starzycka", "Paulina Koczur", "Mariusz Górski",
    "Arkadiusz Lech",
]


def _slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = ROSTER
    print(f"  ozimek roster: {len(names)}  sessions IX: 0 (BIP PRO3W JS-rendered)")

    kad = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [],
        "total_sessions": 0, "total_votes": 0, "total_councilors": len(names),
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
