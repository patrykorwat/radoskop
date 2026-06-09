#!/usr/bin/env python3
"""Średnia struktura wydatków COFOG po klasach miast (benchmark rówieśniczy).

Czyta cities/<slug>/docs/budget.json (z blokiem 'cofog' od build_budget_bestia.py),
grupuje miasta na KLASY o tym samym zakresie zadań i liczy średni udział każdej
funkcji COFOG. To jedyny uczciwy benchmark dla pojedynczego miasta: ta sama metoda
(BeSTi@), ten sam szczebel. Eurostat S.1313 odrzucony (miesza szczeble) — patrz
radoskop-premium/strategia/COFOG_BENCHMARK.md.

Klasa z kodu TERYT (config 'teryt'):
  grodzki  = miasto na prawach powiatu (pk 61-99) — pełni też zadania powiatu
  gmina    = gmina miejska/miejsko-wiejska

Średnia = nieważona po miastach (każde miasto = 1 głos), żeby duże nie dominowały.
Liczona z NAJNOWSZEGO roku każdego miasta.

Output: docs/units/cofog_peers.json. Frontend porównuje miasto do średniej jego klasy.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_dzial_cofog import COFOG_LABELS  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
CITIES = HERE / "cities"
OUT = HERE / "docs" / "units" / "cofog_peers.json"


def city_class(teryt: str) -> str | None:
    if not teryt or len(teryt) < 4:
        return None
    pk = teryt[2:4]
    return "grodzki" if (pk.isdigit() and int(pk) >= 61) else "gmina"


def latest_cofog(budget: dict) -> tuple[str, dict] | None:
    """Zwraca (rok, {GFxx: pct}) z najnowszego roku w budget.cofog."""
    cof = budget.get("cofog") or {}
    if not cof:
        return None
    year = max(cof.keys())
    shares = {c["code"]: c["pct"] for c in cof[year] if c.get("pct") is not None}
    return (year, shares) if shares else None


def main() -> int:
    # klasa -> GFxx -> lista udziałów (po miastach)
    acc: dict[str, dict[str, list[float]]] = {"grodzki": {}, "gmina": {}}
    used: dict[str, list[str]] = {"grodzki": [], "gmina": []}
    years: list[str] = []
    # Agregat łączny po wszystkich miastach (najnowszy rok każdego).
    agg = {"revenue": 0.0, "expenditure": 0.0, "debt": 0.0, "n": 0}
    agg_years: list[str] = []

    for cfg_path in sorted(CITIES.glob("*/config.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (cfg.get("locale") or "pl").lower() != "pl" or cfg.get("disabled"):
            continue
        klass = city_class(cfg.get("teryt", ""))
        if not klass:
            continue
        bpath = cfg_path.parent / "docs" / "budget.json"
        if not bpath.exists():
            continue
        try:
            budget = json.loads(bpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Agregat: najnowszy rok z totals.
        tt = budget.get("totals") or []
        if tt:
            lt = max(tt, key=lambda t: t.get("year", ""))
            agg["revenue"] += lt.get("revenue") or 0
            agg["expenditure"] += lt.get("expenditure") or 0
            agg["debt"] += lt.get("debt") or 0
            agg["n"] += 1
            agg_years.append(lt.get("year", ""))
        lc = latest_cofog(budget)
        if not lc:
            continue
        year, shares = lc
        years.append(year)
        used[klass].append(cfg_path.parent.name)
        for gf, pct in shares.items():
            acc[klass].setdefault(gf, []).append(pct)

    out = {
        "generated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "nieważona średnia udziałów COFOG po miastach klasy, najnowszy rok każdego miasta",
        "year_range": (f"{min(years)}–{max(years)}" if years else None),
        "classes": {},
    }
    for klass in ("grodzki", "gmina"):
        funcs = {}
        for gf, vals in sorted(acc[klass].items()):
            funcs[gf] = {
                "name": COFOG_LABELS.get(gf, gf),
                "pct_mean": round(statistics.mean(vals), 1),
                "pct_median": round(statistics.median(vals), 1),
                "n": len(vals),
            }
        out["classes"][klass] = {"n_cities": len(used[klass]), "functions": funcs}
        print(f"{klass}: {len(used[klass])} miast")
        for gf in sorted(funcs, key=lambda g: -funcs[g]["pct_mean"]):
            print(f"  {gf} {funcs[gf]['name'][:34]:34} śr {funcs[gf]['pct_mean']}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"zapisano {OUT}")

    # Writeback: wstrzyknij średnią klasy do budget.json każdego miasta (pole
    # cofog_peer), żeby widok budżetu miał porównanie w jednym pliku, bez fetchu
    # do apexu i bez logiki klas po stronie frontendu.
    wrote = 0
    for klass in ("grodzki", "gmina"):
        funcs_mean = {gf: v["pct_mean"]
                      for gf, v in out["classes"][klass]["functions"].items()}
        peer = {"class": klass, "n_cities": out["classes"][klass]["n_cities"],
                "functions": funcs_mean}
        for slug in used[klass]:
            bpath = CITIES / slug / "docs" / "budget.json"
            try:
                b = json.loads(bpath.read_text(encoding="utf-8"))
                b["cofog_peer"] = peer
                bpath.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
                wrote += 1
            except Exception:
                pass
    print(f"wstrzyknięto cofog_peer do {wrote} budżetów")

    # Agregat łączny (landing): suma po monitorowanych miastach.
    totals_out = {
        "generated": out["generated"],
        "n_cities": agg["n"],
        "year_range": (f"{min(agg_years)}–{max(agg_years)}"
                       if agg_years and min(agg_years) != max(agg_years)
                       else (agg_years[0] if agg_years else None)),
        "revenue": round(agg["revenue"], 2),
        "expenditure": round(agg["expenditure"], 2),
        "deficit": round(agg["revenue"] - agg["expenditure"], 2),
        "debt": round(agg["debt"], 2),
    }
    (OUT.parent / "budget_totals.json").write_text(
        json.dumps(totals_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"agregat: {agg['n']} miast, wydatki {agg['expenditure']/1e9:.1f} mld, "
          f"deficyt {(agg['revenue']-agg['expenditure'])/1e9:.2f} mld, dług {agg['debt']/1e9:.1f} mld")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
