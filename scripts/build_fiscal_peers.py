#!/usr/bin/env python3
"""Mediany wskaźników MF (OSF) po klasach JST — benchmark rówieśniczy.

Czyta cities/<slug>/docs/fiscal-indicators.json (build_fiscal_indicators.py),
grupuje po klasie z TERYT (grodzki = miasto na prawach powiatu, gmina = reszta
— dokładnie jak build_cofog_peers) i liczy medianę/kwartyle każdego wskaźnika
per rok. To odpowiednik tablic grupowych MF (średnia/min/max per kategoria),
ale liczony z NAJŚWIEŻSZYCH sprawozdań dla miast objętych Radoskopem — MF
publikuje migawkę z opóźnieniem ~12 mies. i tylko do 2023.

Output: docs/units/fiscal_peers.json
Klucz: {class: {year: {indicator: {n, min, q1, med, q3, max}}}}
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CITIES = HERE / "cities"
OUT = HERE / "docs" / "units" / "fiscal_peers.json"

INDICATORS = ["WB1", "WB2", "WB3", "WB4", "WB5", "WB6", "WB7", "WB8", "WB9",
              "WB10", "WB11", "WB12", "WL1", "WL2", "WL3", "WL4", "WL5",
              "WZ1", "WZ2", "WZ3", "WZ4", "WZ5", "WZ6"]


def city_class(teryt: str) -> str | None:
    if not teryt or len(teryt) < 4:
        return None
    pk = teryt[2:4]
    return "grodzki" if (pk.isdigit() and int(pk) >= 61) else "gmina"


def quartiles(vals: list[float]) -> dict:
    vals = sorted(vals)
    med = statistics.median(vals)
    n = len(vals)
    if n >= 4:
        half = n // 2
        q1 = statistics.median(vals[:half])
        q3 = statistics.median(vals[half:] if n % 2 == 0 else vals[half + 1:])
    else:
        q1 = q3 = med
    return {"n": n, "min": round(vals[0], 2), "q1": round(q1, 2),
            "med": round(med, 2), "q3": round(q3, 2), "max": round(vals[-1], 2)}


def main() -> int:
    acc: dict[str, dict[str, dict[str, list[float]]]] = {"grodzki": {}, "gmina": {}}
    used: dict[str, set[str]] = {"grodzki": set(), "gmina": set()}

    for cfg_path in sorted(CITIES.glob("*/config.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (cfg.get("country") or cfg.get("locale") or "pl").lower() != "pl" or cfg.get("disabled"):
            continue
        klass = city_class(cfg.get("teryt", ""))
        if not klass:
            continue
        fpath = cfg_path.parent / "docs" / "fiscal-indicators.json"
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        for yr in data.get("years") or []:
            y = str(yr.get("year"))
            if not y:
                continue
            bucket = acc[klass].setdefault(y, {})
            got = False
            for ind in INDICATORS:
                v = yr.get(ind)
                if isinstance(v, (int, float)):
                    bucket.setdefault(ind, []).append(float(v))
                    got = True
            if got:
                used[klass].add(cfg_path.parent.name)

    out = {
        "generated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": ("Mediany wskaźników MF OSF (WB/WL/WZ) liczonych przez "
                   "build_fiscal_indicators.py po miastach Radoskop, per klasa JST"),
        "methodology": ("Ministerstwo Finansów, Wskaźniki do oceny sytuacji "
                        "finansowej JST (definice wskaźników); mediana klasy "
                        "jako benchmark rówieśniczy"),
        "classes": {},
    }
    for klass, years in acc.items():
        out["classes"][klass] = {
            "n_cities": len(used[klass]),
            "years": {y: {ind: quartiles(vals) for ind, vals in sorted(iv.items()) if vals}
                      for y, iv in sorted(years.items())},
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"zapisano {OUT}: " + ", ".join(
        f"{k}: {v['n_cities']} miast, {len(v['years'])} lat" for k, v in out["classes"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
