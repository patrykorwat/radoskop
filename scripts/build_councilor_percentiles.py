#!/usr/bin/env python3
"""
Reads cross-city.json and produces councilor-percentiles.json.

For each population tier, computes percentile breakpoints (p10..p90) for
frekwencja, aktywnosc, and zgodnosc_z_klubem. Also maps every city slug to
its tier so the profile builder knows which tier to use.

Population tiers (PL + foreign cities):
  small   < 50k
  medium  50k – 200k
  large   200k – 500k
  metro   > 500k

Usage:
    python build_councilor_percentiles.py
    python build_councilor_percentiles.py --cross-city /path/to/cross-city.json --out /path/to/councilor-percentiles.json
"""

import argparse
import json
from pathlib import Path


TIERS = [
    ('small',  0,       50_000,  'poniżej 50 tys. mieszkańców'),
    ('medium', 50_000,  200_000, '50–200 tys. mieszkańców'),
    ('large',  200_000, 500_000, '200–500 tys. mieszkańców'),
    ('metro',  500_000, None,    'powyżej 500 tys. mieszkańców'),
]


def classify_tier(population: int | None) -> str | None:
    if population is None:
        return None
    for slug, lo, hi, _ in TIERS:
        if population >= lo and (hi is None or population < hi):
            return slug
    return None


def percentile(sorted_values: list[float], p: int) -> float:
    """Return the p-th percentile (0–100) of a pre-sorted list."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = p / 100 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac, 1)


def compute_tier_stats(values: list[float]) -> dict:
    s = sorted(v for v in values if v is not None)
    if not s:
        return {}
    return {
        'p10': percentile(s, 10),
        'p25': percentile(s, 25),
        'p50': percentile(s, 50),
        'p75': percentile(s, 75),
        'p90': percentile(s, 90),
        'n': len(s),
    }


def councilor_percentile_rank(value: float, sorted_values: list[float]) -> int:
    """Return what % of councilors this value is higher than (0–100)."""
    if not sorted_values:
        return 0
    below = sum(1 for v in sorted_values if v < value)
    return round(below / len(sorted_values) * 100)


def build_percentiles(cross_city_path: Path, output_path: Path) -> None:
    with open(cross_city_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    councilors = data.get('councilors', [])
    cities = data.get('cities', [])

    # Build city_slug → population map from the cities list.
    city_pop: dict[str, int | None] = {}
    for c in cities:
        slug = c.get('slug') or ''
        city_pop[slug] = c.get('population')

    # Bucket councilor values by tier.
    tier_buckets: dict[str, dict[str, list[float]]] = {
        t[0]: {'frekwencja': [], 'aktywnosc': [], 'zgodnosc_z_klubem': []}
        for t in TIERS
    }

    # Also track which cities fall into each tier.
    tier_cities: dict[str, set[str]] = {t[0]: set() for t in TIERS}

    for c in councilors:
        pop = c.get('population') or city_pop.get(c.get('city_slug', ''))
        tier = classify_tier(pop)
        if tier is None:
            continue
        tier_buckets[tier]['frekwencja'].append(c.get('frekwencja', 0))
        tier_buckets[tier]['aktywnosc'].append(c.get('aktywnosc', 0))
        tier_buckets[tier]['zgodnosc_z_klubem'].append(c.get('zgodnosc_z_klubem', 0))
        if c.get('city_slug'):
            tier_cities[tier].add(c['city_slug'])

    # Build tier stats + sorted value lists (for per-councilor percentile rank).
    tiers_out: dict[str, dict] = {}
    for tier_slug, lo, hi, label in TIERS:
        buckets = tier_buckets[tier_slug]
        n = len(buckets['frekwencja'])
        if n == 0:
            continue
        tiers_out[tier_slug] = {
            'label': label,
            'n_councilors': n,
            'n_cities': len(tier_cities[tier_slug]),
            'frekwencja': compute_tier_stats(buckets['frekwencja']),
            'aktywnosc': compute_tier_stats(buckets['aktywnosc']),
            'zgodnosc_z_klubem': compute_tier_stats(buckets['zgodnosc_z_klubem']),
            'sorted_frekwencja': sorted(buckets['frekwencja']),
            'sorted_aktywnosc': sorted(buckets['aktywnosc']),
            'sorted_zgodnosc_z_klubem': sorted(buckets['zgodnosc_z_klubem']),
        }

    # Map every city slug to its tier.
    city_tiers: dict[str, str] = {}
    for slug, pop in city_pop.items():
        t = classify_tier(pop)
        if t:
            city_tiers[slug] = t
    # Also pick up cities that only appear in the councilor list.
    for c in councilors:
        slug = c.get('city_slug', '')
        if slug and slug not in city_tiers:
            pop = c.get('population') or city_pop.get(slug)
            t = classify_tier(pop)
            if t:
                city_tiers[slug] = t

    output = {
        'tiers': tiers_out,
        'city_tiers': city_tiers,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Built councilor-percentiles.json")
    for tier_slug, stats in tiers_out.items():
        print(f"  {tier_slug}: {stats['n_councilors']} radnych, {stats['n_cities']} miast")
    print(f"  city_tiers mapped: {len(city_tiers)} cities")
    print(f"  Output: {output_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--cross-city',
        default=None,
        help='Path to cross-city.json (default: radoskop/docs/cross-city.json)',
    )
    parser.add_argument(
        '--out',
        default=None,
        help='Output path (default: radoskop/docs/councilor-percentiles.json)',
    )
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent.parent
    cross_city_path = Path(args.cross_city) if args.cross_city else base / 'radoskop' / 'docs' / 'cross-city.json'
    out_path = Path(args.out) if args.out else base / 'radoskop' / 'docs' / 'councilor-percentiles.json'

    build_percentiles(cross_city_path, out_path)
