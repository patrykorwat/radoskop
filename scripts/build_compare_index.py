#!/usr/bin/env python3
"""Buduje compare-index.json: kompaktowy, zdolnościowy (capability based) zbiór
danych do porównywarki miast w widoku miasta.

Idea: porównanie dwóch miast nie filtruje par, tylko renderuje moduły analiz,
gdzie KAŻDY moduł sam deklaruje, jakich zdolności (capabilities) potrzebuje.
Zdolność miasta jest WYKRYWANA AUTOMATYCZNIE z dostępnych danych, bez ręcznych
flag w config.json. Dzięki temu:
  - dodanie miasta zapala tylko te moduły, dla których ma realne dane,
  - dodanie nowego modułu zapala go dla każdej pary, która ma komplet danych,
  - budżet (i każde przyszłe źródło) wchodzi sam, gdy tylko dane się pojawią.

Wejście (wszystko z radoskop/docs/, plus per-miasto budget.json):
  - cross-city.json   -> metryki głosowań/frekwencji/zgodności per miasto
  - cities.json       -> tożsamość: country, voivodeship, population, lat/lon
  - comparison.json   -> rozkład klubów per miasto (council_composition)
  - cities/<slug>/docs/budget.json -> budżet (capability budget)

Wyjście: radoskop/docs/compare-index.json — JEDEN plik dla wszystkich miast.
run_pipeline kopiuje go do docs/ każdego miasta, więc frontend pobiera go
same-origin (bez CORS między subdomenami), tak samo jak percentile baked do
profiles.json.

Wartości walut NIE są przeliczane między sobą. Budżet porównujemy per capita
i z jawną adnotacją waluty; surowe sumy zostają w walucie miasta.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# --- Rejestr modułów (źródło prawdy współdzielone z frontendem) ---------------
# id        : klucz modułu
# requires  : lista capability, które OBA miasta muszą mieć, by moduł się pokazał
# label_*   : podpisy UI (frontend i tak ma własne i18n, tu dla czytelności)
MODULE_REGISTRY: List[Dict[str, Any]] = [
    {"id": "voting_activity", "requires": ["voting_activity"]},
    {"id": "attendance", "requires": ["attendance"]},
    {"id": "club_cohesion", "requires": ["club_cohesion"]},
    {"id": "council_composition", "requires": ["council_composition"]},
    {"id": "demographics", "requires": ["demographics"]},
    {"id": "budget", "requires": ["budget"]},
]


def _num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: nie udało się wczytać {path}: {e}")
        return None


def _index_by_slug(rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        slug = r.get("slug")
        if slug:
            out[slug] = r
    return out


def load_budget_summary(city_dir: Path) -> Optional[Dict[str, Any]]:
    """Kompaktuje budget.json miasta do podsumowania porównywalnego.

    Bierze NAJNOWSZY rok nie-estymowany (fallback: najnowszy w ogóle). Liczby
    bezwzględne zostają w walucie miasta; normalizacja per capita liczona jest
    wyżej, gdy znana jest populacja.
    """
    budget = _load_json(city_dir / "docs" / "budget.json")
    if not budget:
        return None
    totals = budget.get("totals") or []
    if not totals:
        return None

    real = [t for t in totals if not t.get("estimated")]
    pick = max(real or totals, key=lambda t: t.get("year", 0))
    year = pick.get("year")

    summary: Dict[str, Any] = {
        "currency": budget.get("currency", ""),
        "year": year,
        "revenue": _num(pick.get("revenue")),
        "expenditure": _num(pick.get("expenditure")),
        "deficit": _num(pick.get("deficit")),
        "estimated": bool(pick.get("estimated", False)),
        "years_covered": len(totals),
    }

    # Udział największych działów (porównywalny mimo różnych taksonomii: liczymy
    # KONCENTRACJĘ wydatków w top-3, nie mapujemy nazw kategorii między krajami).
    cats = (budget.get("categories") or {}).get(str(year)) or []
    total_cat = sum(_num(c.get("amount")) or 0 for c in cats)
    if cats and total_cat > 0:
        top = sorted(cats, key=lambda c: -(_num(c.get("amount")) or 0))[:3]
        summary["top_categories"] = [
            {"name": c.get("name"), "share": round((_num(c.get("amount")) or 0) / total_cat * 100, 1)}
            for c in top
        ]
    return summary


def detect_capabilities(metrics: Dict[str, Any]) -> List[str]:
    """Wykrywa zdolności z obecności i sensowności danych. Bez flag w config."""
    caps: List[str] = []
    va = metrics.get("voting_activity") or {}
    if (va.get("votes") or 0) > 0:
        caps.append("voting_activity")
    att = metrics.get("attendance") or {}
    if (att.get("avg_frekwencja") or 0) > 0:
        caps.append("attendance")
    coh = metrics.get("club_cohesion") or {}
    if (coh.get("avg_zgodnosc") or 0) > 0:
        caps.append("club_cohesion")
    comp = metrics.get("council_composition") or {}
    if (comp.get("councilors") or 0) > 0 and comp.get("clubs"):
        caps.append("council_composition")
    demo = metrics.get("demographics") or {}
    if (demo.get("population") or 0) > 0:
        caps.append("demographics")
    if metrics.get("budget"):
        caps.append("budget")
    return caps


def build_city_record(
    slug: str,
    cc: Dict[str, Any],
    ci: Dict[str, Any],
    comp: Optional[Dict[str, Any]],
    cities_root: Path,
) -> Optional[Dict[str, Any]]:
    """Składa rekord jednego miasta: tożsamość + metryki pogrupowane wg modułu."""
    name = (cc or {}).get("name") or (ci or {}).get("name") or slug
    url = (cc or {}).get("url") or (ci or {}).get("url") or ""
    population = _num((ci or {}).get("population")) or _num((cc or {}).get("population"))
    country = (ci or {}).get("country")
    voivodeship = (ci or {}).get("voivodeship")

    sessions = _num((cc or {}).get("session_count")) or _num((ci or {}).get("sessions")) or 0
    votes = _num((cc or {}).get("vote_count")) or _num((ci or {}).get("votes")) or 0
    councilors = _num((cc or {}).get("councilor_count")) or _num((ci or {}).get("councilors")) or 0
    frekwencja = _num((cc or {}).get("avg_frekwencja"))
    if frekwencja is None:
        frekwencja = _num((ci or {}).get("frekwencja"))
    zgodnosc = _num((cc or {}).get("avg_zgodnosc"))
    if zgodnosc is None:
        zgodnosc = _num((ci or {}).get("zgodnosc"))
    rebellions = _num((cc or {}).get("avg_rebellions"))
    if rebellions is None and comp:
        rebellions = _num(comp.get("avg_rebellions"))

    metrics: Dict[str, Any] = {}

    # voting_activity ---------------------------------------------------------
    if votes and votes > 0:
        metrics["voting_activity"] = {
            "sessions": int(sessions or 0),
            "votes": int(votes),
            "votes_per_session": round(votes / sessions, 1) if sessions else None,
        }

    # attendance --------------------------------------------------------------
    if frekwencja and frekwencja > 0:
        metrics["attendance"] = {"avg_frekwencja": round(frekwencja, 1)}

    # club_cohesion -----------------------------------------------------------
    if zgodnosc and zgodnosc > 0:
        cohesion: Dict[str, Any] = {"avg_zgodnosc": round(zgodnosc, 1)}
        if rebellions is not None:
            cohesion["avg_rebellions"] = round(rebellions, 1)
        metrics["club_cohesion"] = cohesion

    # council_composition -----------------------------------------------------
    clubs = (comp or {}).get("clubs") if comp else None
    if councilors and councilors > 0:
        comp_block: Dict[str, Any] = {"councilors": int(councilors)}
        if clubs:
            sizes = {k: (v.get("count") if isinstance(v, dict) else v) for k, v in clubs.items()}
            sizes = {k: v for k, v in sizes.items() if v}
            if sizes:
                largest = max(sizes.values())
                comp_block["clubs"] = sizes
                comp_block["num_clubs"] = len(sizes)
                comp_block["largest_club_share"] = round(largest / sum(sizes.values()) * 100, 1)
        metrics["council_composition"] = comp_block

    # demographics ------------------------------------------------------------
    if population and population > 0:
        demo: Dict[str, Any] = {"population": int(population)}
        if councilors:
            demo["councilors_per_100k"] = round(councilors / population * 100_000, 2)
        if votes:
            demo["votes_per_100k"] = round(votes / population * 100_000, 1)
        metrics["demographics"] = demo

    # budget ------------------------------------------------------------------
    budget = load_budget_summary(cities_root / slug)
    if budget:
        if population and budget.get("expenditure"):
            budget["per_capita_expenditure"] = round(budget["expenditure"] / population, 1)
        if population and budget.get("revenue"):
            budget["per_capita_revenue"] = round(budget["revenue"] / population, 1)
        metrics["budget"] = budget

    if not metrics:
        return None

    return {
        "slug": slug,
        "name": name,
        "url": url,
        "country": country,
        "voivodeship": voivodeship,
        "population": int(population) if population else None,
        "capabilities": detect_capabilities(metrics),
        "metrics": metrics,
    }


def build_compare_index(docs_dir: Path, cities_root: Path, output_path: Path) -> Dict[str, Any]:
    cross = _load_json(docs_dir / "cross-city.json") or {}
    cities = _load_json(docs_dir / "cities.json") or []
    comparison = _load_json(docs_dir / "comparison.json") or {}

    cc_by_slug = _index_by_slug(cross.get("cities"))
    ci_by_slug = _index_by_slug(cities if isinstance(cities, list) else cities.get("cities"))
    comp_by_slug = _index_by_slug(comparison.get("cities") if isinstance(comparison, dict) else None)

    all_slugs = sorted(set(cc_by_slug) | set(ci_by_slug))

    records: List[Dict[str, Any]] = []
    for slug in all_slugs:
        rec = build_city_record(
            slug,
            cc_by_slug.get(slug, {}),
            ci_by_slug.get(slug, {}),
            comp_by_slug.get(slug),
            cities_root,
        )
        if rec:
            records.append(rec)

    records.sort(key=lambda r: r["name"])

    output = {
        "generated": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "modules": MODULE_REGISTRY,
        "cities": records,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    return output


def main() -> None:
    here = Path(__file__).resolve().parent
    radoskop = here.parent
    parser = argparse.ArgumentParser(description="Build compare-index.json")
    parser.add_argument("--docs", default=str(radoskop / "docs"), help="Katalog z cross-city.json/cities.json/comparison.json")
    parser.add_argument("--cities", default=str(radoskop / "cities"), help="Katalog cities/<slug>/docs/budget.json")
    parser.add_argument("--out", default=str(radoskop / "docs" / "compare-index.json"))
    args = parser.parse_args()

    out = build_compare_index(Path(args.docs), Path(args.cities), Path(args.out))
    n = len(out["cities"])
    caps = sorted({c for r in out["cities"] for c in r["capabilities"]})
    print(f"compare-index: {n} miast, capability spotykane: {caps}")
    print(f"Zapisano: {args.out}")


if __name__ == "__main__":
    main()
