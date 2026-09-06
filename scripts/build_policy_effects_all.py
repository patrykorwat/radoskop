#!/usr/bin/env python3
"""Skutki polityki — przebieg krajowy (driver dla build_policy_effects).

Effekt pojedynczego miasta wymaga mediany rówieśniczej ze WSZYSTKICH miast;
uruchamianie build_policy_effects.py per miasto z --peers-glob czytałoby
383 pliki 383 razy. Ten driver:
  1. wczytuje fiscal-indicators.json wszystkich miast (jedno przejście),
  2. liczy medianę rówieśniczą per wskaznik per rok (jedna mediana krajowa —
     dopracowanie: per klasa JST, patrz build_fiscal_peers),
  3. dla kazdego miasta z data.json + fiscal pisze cities/{slug}/docs/
     policy-effects.json (serwowane z S3 per miasto),
  4. agreguje zdarzenia z chipem sygnaturowym do docs/units/policy_effects.json
     (krajowy indeks feedu Radar; serwisowany jako _main/units/policy_effects.json).

Uruchamiac w budzecie pipeline'u (run_scrape_budgets) po build_fiscal_indicators.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_policy_effects import (  # noqa: E402
    ALL_INDICATORS, EFFECTS, EFFECTIVE_NEXT_YEAR, SIGNATURE,
    adopted, classify, load_votes, window,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="katalog repo radoskop (z cities/)")
    ap.add_argument("--out-index", default=None,
                    help="sciezka indeksu krajowego (domyslnie docs/units/policy_effects.json)")
    ap.add_argument("--staging", default=None,
                    help="zamiast pisac do cities/{slug}/docs/ (repo root-owned poza "
                         "kontenerem), zrzuc pliki miasta do STAGING/{slug}/policy-effects.json")
    args = ap.parse_args()
    repo = Path(args.repo)
    cities = repo / "cities"

    # 1. fiscal per miasto + 2. mediana krajowa
    fiscals: dict[str, dict[str, dict]] = {}
    for f in sorted(cities.glob("*/docs/fiscal-indicators.json")):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        yrs = {y["year"]: y for y in d.get("years", []) if y.get("year")}
        if yrs:
            fiscals[f.parts[-3]] = yrs
    if not fiscals:
        print("brak fiscal-indicators.json pod", cities, file=sys.stderr)
        return 1

    acc: dict[str, dict[str, list[float]]] = {}
    for yrs in fiscals.values():
        for y, row in yrs.items():
            for ind in ALL_INDICATORS:
                v = row.get(ind)
                if isinstance(v, (int, float)):
                    acc.setdefault(y, {}).setdefault(ind, []).append(v)
    import statistics
    peer_med = {y: {i: statistics.median(xs) for i, xs in row.items() if xs}
                for y, row in acc.items()}

    # 3. per miasto
    out_index_events: list[dict] = []
    n_files = 0
    for slug, fiscal in sorted(fiscals.items()):
        docs = cities / slug / "docs"
        has_votes = (docs / "data.json").is_file() or any(docs.glob("kadencja-*.json"))
        if not has_votes:
            continue
        votes = load_votes(str(docs))
        events = []
        for v in votes:
            if not adopted(v):
                continue
            cat = classify(v.get("topic", ""))
            if not cat:
                continue
            date = str(v.get("session_date") or "")
            if len(date) < 4 or not date[:4].isdigit():
                continue
            y0 = int(date[:4])
            y_eff = y0 + 1 if cat in EFFECTIVE_NEXT_YEAR else y0
            inds = EFFECTS[cat]
            pre = [str(y_eff - 2), str(y_eff - 1)]
            post = [str(y_eff), str(y_eff + 1)]
            base = window(fiscal, inds, pre)
            after = window(fiscal, inds, post)
            if all(x is None for x in after.values()) and all(x is None for x in base.values()):
                continue
            pbase = window(peer_med, inds, pre)
            pafter = window(peer_med, inds, post)
            delta = {}
            for ind in inds:
                d = dv = None
                b_i, a_i = base.get(ind), after.get(ind)
                if isinstance(b_i, float) and isinstance(a_i, float):
                    d = a_i - b_i
                pb_i, pa_i = pbase.get(ind), pafter.get(ind)
                if isinstance(pb_i, float) and isinstance(pa_i, float):
                    dv = pa_i - pb_i
                delta[ind] = {"miasto": round(d, 4) if d is not None else None,
                              "rowiesnicy": round(dv, 4) if dv is not None else None,
                              "ponad_trend": round(d - dv, 4) if d is not None and dv is not None else None}
            sig = SIGNATURE.get(cat)
            chip = None
            if sig and sig in delta and delta[sig]["ponad_trend"] is not None:
                chip = {"indicator": sig, "value": delta[sig]["ponad_trend"]}
            ev = {
                "vote_id": v["id"], "date": v.get("session_date"),
                "session_number": v.get("session_number"), "topic": v.get("topic"),
                "category": cat, "effective_year": y_eff,
                "counts": v.get("counts"), "source_url": v.get("source_url"),
                "signature": chip,
                "window": {"przed": pre, "po": post},
                "przed": base, "po": after, "delta": delta,
            }
            events.append(ev)
            if chip:
                out_index_events.append({**{k: ev[k] for k in
                                            ("vote_id", "date", "effective_year", "topic",
                                             "category", "counts", "signature")},
                                         "city": slug})
        if not events:
            continue
        events.sort(key=lambda e: str(e["date"] or ""))
        series_city = {ind: {y: r[ind] for y, r in fiscal.items()
                             if isinstance(r.get(ind), (int, float))}
                       for ind in ALL_INDICATORS}
        series_peer = {ind: {y: r[ind] for y, r in peer_med.items()
                             if isinstance(r.get(ind), (int, float))}
                       for ind in ALL_INDICATORS}
        out = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "uchwaly z BIP (topic glosowania) x wskazniki OSF MF (BeSTi@) — build_policy_effects.py",
            "method": "delta srednia(eff..eff+1) vs srednia(eff-2..eff-1), wskaznik MF; mediana rowiesnicza krajowa. Asocjacja czasowa, nie kauzacja.",
            "caveat": "Wspolwystepowanie wielu czynnikow; pojedyncza uchwala rzadko jest jedynym sprawca zmiany wskaznika.",
            "peer_basis": f"mediana wskaznikowa z {len(fiscals)} miast",
            "series": {"miasto": series_city, "mediana": series_peer},
            "events": events,
        }
        staging_out = (Path(args.staging) / slug) if args.staging else docs
        staging_out.mkdir(parents=True, exist_ok=True)
        (staging_out / "policy-effects.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        n_files += 1

    # 4. indeks krajowy (feed Radar) — tylko zdarzenia z chipem
    out_index_events.sort(key=lambda e: str(e["date"] or ""), reverse=True)
    out_index = args.out_index or str(repo / "docs" / "units" / "policy_effects.json")
    idx = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "zdarzenia policyjne z mierzalnym efektem na wskazniku sygnaturowym MF (ponad_trend vs mediana). Asocjacja, nie kauzacja.",
        "n_cities": n_files,
        "total": len(out_index_events),
        "items": out_index_events,
    }
    Path(out_index).parent.mkdir(parents=True, exist_ok=True)
    Path(out_index).write_text(json.dumps(idx, ensure_ascii=False))
    print(f"policy-effects: {n_files} miast, {len(out_index_events)} zdarzen z chipem; "
          f"indeks: {out_index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
