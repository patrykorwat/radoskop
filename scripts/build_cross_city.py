#!/usr/bin/env python3
"""
Aggregates data from all Radoskop city deployments and generates a cross-city comparison JSON file.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_clubs import club_has_line  # noqa: E402


def get_city_name_from_dir(dirname: str) -> str:
    """Convert directory name like 'radoskop-gdansk' to 'Gdańsk'."""
    # For special cases
    name_mapping = {
        'gdansk': 'Gdańsk',
        'gdynia': 'Gdynia',
        'sopot': 'Sopot',
        'warszawa': 'Warszawa',
        'krakow': 'Kraków',
        'wroclaw': 'Wrocław',
        'poznan': 'Poznań',
        'szczecin': 'Szczecin',
        'lublin': 'Lublin',
        'lodz': 'Łódź',
        'bialystok': 'Białystok',
        'bydgoszcz': 'Bydgoszcz',
        'katowice': 'Katowice',
    }

    city_slug = dirname.replace('radoskop-', '').lower()
    return name_mapping.get(city_slug, city_slug.title())


def load_city_data(city_dir: Path) -> Dict[str, Any] | None:
    """Load configuration and data for a single city."""
    config_path = city_dir / 'config.json'
    data_path = city_dir / 'docs' / 'data.json'

    if not config_path.exists() or not data_path.exists():
        return None

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        with open(data_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return {
            'config': config,
            'data': data,
            'city_dir': city_dir
        }
    except Exception as e:
        print(f"Error loading {city_dir}: {e}")
        return None


def get_kadencja_data(city_data: Dict[str, Any]) -> Dict[str, Any] | None:
    """Get the default kadencja data for a city."""
    data = city_data['data']
    default_kadencja_id = data.get('default_kadencja', '') or '2024-2029'
    city_dir = city_data['city_dir']

    # Try to load from kadencja-{id}.json file first (most complete)
    kadencja_file = city_dir / 'docs' / f'kadencja-{default_kadencja_id}.json'
    if kadencja_file.exists():
        try:
            with open(kadencja_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {kadencja_file}: {e}")

    # Fallback: try to find in data.json
    for kadencja in data.get('kadencje', []):
        if kadencja['id'] == default_kadencja_id and kadencja.get('councilors'):
            return kadencja

    # If default not found, try the latest one from data with councilors
    kadencje = data.get('kadencje', [])
    for kadencja in reversed(kadencje):
        if kadencja.get('councilors'):
            return kadencja

    return None


def load_profiles(city_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load profiles.json to get profile slugs for building profile URLs."""
    profiles_path = city_dir / 'docs' / 'profiles.json'

    if not profiles_path.exists():
        return {}

    try:
        with open(profiles_path, 'r', encoding='utf-8') as f:
            profiles_data = json.load(f)

        # Create a name -> slug mapping
        name_to_slug = {}
        if isinstance(profiles_data, dict) and 'profiles' in profiles_data:
            for profile in profiles_data['profiles']:
                name_to_slug[profile['name']] = profile['slug']

        return name_to_slug
    except Exception as e:
        print(f"Error loading profiles from {city_dir}: {e}")
        return {}


def load_population_lookup(base_path: Path) -> dict[str, int]:
    """Load slug→population from radoskop/docs/cities.json."""
    cities_json = base_path / 'radoskop' / 'docs' / 'cities.json'
    if not cities_json.exists():
        return {}
    try:
        with open(cities_json, 'r', encoding='utf-8') as f:
            entries = json.load(f)
        return {e['slug']: e['population'] for e in entries if e.get('population')}
    except Exception as e:
        print(f"Warning: could not load cities.json for population lookup: {e}")
        return {}


def build_cross_city_json(base_path: Path, output_path: Path):
    """Main function to aggregate all city data and build cross-city JSON."""

    # Discover cities. Po migracji do monorepo szukamy w radoskop/cities/{slug}/.
    # Fallback do legacy radoskop-{slug}/ dla repo które jeszcze nie przeszły
    # migracji. Bez tego nowe miasta dodane do cities/ (np. czestochowa, radom)
    # nie wchodziły do cross-city.json bo nie było odpowiadającego sibling
    # `radoskop-{slug}/` dir w workspace root.
    population_lookup = load_population_lookup(base_path)

    city_dirs: list[Path] = []
    monorepo_cities = base_path / 'radoskop' / 'cities'
    if monorepo_cities.is_dir():
        for d in sorted(monorepo_cities.iterdir()):
            if d.is_dir() and (d / 'config.json').exists():
                city_dirs.append(d)
    # Legacy sibling radoskop-{slug}/ (po migracji większość pusta, ale jeśli
    # ktoś jeszcze trzyma stary layout z config.json+docs/data.json to też wpadnie).
    for d in sorted(base_path.iterdir()):
        if d.is_dir() and d.name.startswith('radoskop-') and d.name != 'radoskop-premium':
            slug = d.name[len('radoskop-'):]
            already_in = any(p.name == slug for p in city_dirs)
            if not already_in and (d / 'config.json').exists():
                city_dirs.append(d)

    cities_data = []
    all_councilors = []
    all_clubs = defaultdict(lambda: {'cities': set(), 'frekwencja': [], 'aktywnosc': [], 'zgodnosc': []})

    for city_dir in city_dirs:
        city_info = load_city_data(city_dir)
        if not city_info:
            continue

        config = city_info['config']

        # Miasta z "disabled": true (paris frakcyjny z danymi przykładowymi,
        # rostock zablokowany) nie wchodzą do cross-city porównania.
        if config.get('disabled'):
            print(f"Skipping {city_dir.name}: disabled w config.json")
            continue

        kadencja = get_kadencja_data(city_info)

        if not kadencja:
            print(f"Warning: Could not find default kadencja for {city_dir.name}")
            continue

        city_name = config.get('city_name', get_city_name_from_dir(city_dir.name))
        site_url = config.get('site_url', '')

        # Calculate averages for the city
        councilors = kadencja.get('councilors', [])
        if not councilors:
            continue

        def _avg(key):
            vals = [c.get(key) for c in councilors if isinstance(c.get(key), (int, float))]
            return sum(vals) / len(vals) if vals else None
        avg_frekwencja = _avg('frekwencja')
        avg_aktywnosc = _avg('aktywnosc')
        avg_zgodnosc = _avg('zgodnosc_z_klubem')

        city_slug = city_dir.name
        population = population_lookup.get(city_slug) or config.get('population')

        city_record = {
            'name': city_name,
            'slug': city_slug,
            'url': site_url,
            'population': population,
            'councilor_count': len(councilors),
            'session_count': kadencja.get('total_sessions', 0),
            'vote_count': kadencja.get('total_votes', 0),
            'avg_frekwencja': round(avg_frekwencja, 2) if avg_frekwencja is not None else None,
            'avg_aktywnosc': round(avg_aktywnosc, 2) if avg_aktywnosc is not None else None,
            'avg_zgodnosc': round(avg_zgodnosc, 2) if avg_zgodnosc is not None else None,
        }
        cities_data.append(city_record)

        # Load profiles for building profile URLs
        profiles = load_profiles(city_dir)

        # Collect councilor data
        for councilor in councilors:
            name = councilor.get('name', '')
            club = councilor.get('club', '')

            # Build profile URL
            profile_slug = profiles.get(name, name.lower().replace(' ', '-'))
            profile_url = f"{site_url}/profil/{profile_slug}" if site_url else ''

            councilor_record = {
                'name': name,
                'city': city_name,
                'city_slug': city_slug,
                'city_url': site_url,
                'population': population,
                'club': club,
                'frekwencja': councilor.get('frekwencja', 0),
                'aktywnosc': councilor.get('aktywnosc', 0),
                'zgodnosc_z_klubem': councilor.get('zgodnosc_z_klubem', 0),
                'rebellion_count': councilor.get('rebellion_count', 0),
                'profile_url': profile_url,
            }
            all_councilors.append(councilor_record)

            # Collect club stats
            all_clubs[club]['cities'].add(city_name)
            all_clubs[club]['frekwencja'].append(councilor.get('frekwencja', 0))
            all_clubs[club]['aktywnosc'].append(councilor.get('aktywnosc', 0))
            all_clubs[club]['zgodnosc'].append(councilor.get('zgodnosc_z_klubem', 0))

    # Sort cities by average frekwencja (descending)
    cities_data.sort(key=lambda x: x['avg_frekwencja'], reverse=True)

    # Get top and bottom frekwencja
    sorted_by_frekwencja = sorted(all_councilors, key=lambda x: x['frekwencja'], reverse=True)
    top_frekwencja = sorted_by_frekwencja[:20]
    bottom_frekwencja = sorted_by_frekwencja[-20:]

    # Get top rebellion (most rebellions). Niezrzeszeni / bez klubu nie mają
    # linii klubowej, więc ich rebellion_count (jeśli stare dane go nadmuchały)
    # nie może trafiać do rankingu buntowników.
    sorted_by_rebellion = sorted(
        (c for c in all_councilors if club_has_line(c.get('club'))),
        key=lambda x: x['rebellion_count'], reverse=True,
    )
    top_rebellion = sorted_by_rebellion[:20]

    # Build clubs cross-city data
    clubs_cross_city = {}
    for club, stats in all_clubs.items():
        frekwencja_values = stats['frekwencja']
        aktywnosc_values = stats['aktywnosc']
        zgodnosc_values = stats['zgodnosc']

        clubs_cross_city[club] = {
            'cities': sorted(list(stats['cities'])),
            'avg_frekwencja': round(sum(frekwencja_values) / len(frekwencja_values), 2) if frekwencja_values else 0,
            'avg_aktywnosc': round(sum(aktywnosc_values) / len(aktywnosc_values), 2) if aktywnosc_values else 0,
            'avg_zgodnosc': round(sum(zgodnosc_values) / len(zgodnosc_values), 2) if zgodnosc_values else 0,
            'councilor_count': len(frekwencja_values),
        }

    # Build final output
    output = {
        'generated': datetime.utcnow().isoformat() + 'Z',
        'cities': cities_data,
        'councilors': all_councilors,
        'top_frekwencja': top_frekwencja,
        'bottom_frekwencja': bottom_frekwencja,
        'top_rebellion': top_rebellion,
        'clubs_cross_city': clubs_cross_city,
    }

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Generated cross-city data for {len(cities_data)} cities")
    print(f"Total councilors: {len(all_councilors)}")
    print(f"Total clubs: {len(clubs_cross_city)}")
    print(f"Output written to: {output_path}")


if __name__ == '__main__':
    # Uruchamiamy z dowolnego miejsca: zakładamy strukturę
    # <root>/radoskop/scripts/build_cross_city.py + <root>/radoskop-<city>/
    base_path = Path(__file__).resolve().parent.parent.parent
    output_path = base_path / 'radoskop' / 'docs' / 'cross-city.json'

    build_cross_city_json(base_path, output_path)
