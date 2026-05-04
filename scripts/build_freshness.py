#!/usr/bin/env python3
"""
Buduje manifest świeżości danych dla apex page Radoskopu.

Skanuje `radoskop/cities/{slug}/docs/` (samorząd typu miasto) oraz
`radoskop/assemblies/{slug}/docs/` (samorząd typu województwo) w poszukiwaniu
plików JSON: kadencja-*.json, interpelacje.json, aktualnosci.json,
budget.json, profiles.json, data.json. Dla każdego pliku ustala znacznik
czasu z pola `scraped_at` (jeśli istnieje) albo z mtime pliku jako
fallback. Zapisuje zbiorczy manifest do
`radoskop/docs/swiezosc/data.json`.

Strona /swiezosc/index.html fetchuje ten manifest i renderuje macierz
miasto/sejmik × źródło danych.

Użycie:
    python3 build_freshness.py
    python3 build_freshness.py --workspace /repos --output radoskop/docs/swiezosc/data.json

W pipeline NAS uruchamiane po build_*_index.py i generate_main_manifest.py,
przed deploy_main_page().
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Definicje źródeł danych do pokazania w macierzy. Współdzielone przez
# miasta i sejmiki (te same nazwy plików, ten sam schemat).
SOURCES: list[dict[str, Any]] = [
    {
        "id": "kadencja",
        "label": "Głosowania",
        "description": "Sesje, głosowania, frekwencja, kadencja {id}",
        "kind": "kadencja_glob",
    },
    {
        "id": "interpelacje",
        "label": "Interpelacje",
        "description": "Interpelacje radnych",
        "kind": "file",
        "file": "interpelacje.json",
    },
    {
        "id": "aktualnosci",
        "label": "Aktualności",
        "description": "Feed sesji i aktywności",
        "kind": "file",
        "file": "aktualnosci.json",
    },
    {
        "id": "profiles",
        "label": "Profile",
        "description": "Profile radnych z biogramami",
        "kind": "file",
        "file": "profiles.json",
    },
    {
        "id": "budget",
        "label": "Budżet",
        "description": "Wydatki, dochody, plan finansowy",
        "kind": "file",
        "file": "budget.json",
    },
]


# Definicje poziomów samorządu (ich katalogów na dysku i nazwy ekranowej).
LEVELS: list[dict[str, Any]] = [
    {
        "id": "city",
        "label": "Miasta",
        "subdir": "cities",
        "samorzad_type": "miasto",
        "name_field": "city_name",
        "meta_csv": "cities-meta.csv",
    },
    {
        "id": "assembly",
        "label": "Sejmiki województw",
        "subdir": "assemblies",
        "samorzad_type": "wojewodztwo",
        "name_field": "rada_name",
        "meta_csv": "assemblies-meta.csv",
    },
]


def utc_iso(ts: float) -> str:
    """Zamień POSIX timestamp na ISO 8601 w UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Any | None:
    """Bezpieczne wczytanie JSON, zwraca None na błąd lub pusty."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def is_payload_empty(data: Any) -> bool:
    """Sprawdź czy JSON faktycznie zawiera dane (poza meta typu scraped_at).

    Plik istnieje ale zawiera `[]`, `{}`, `{"items": []}`, `{"profiles": []}`
    itp. powinien być traktowany jako brak danych. Inaczej manifest pokazuje
    puste szablony jako "świeże".
    """
    if data is None:
        return True
    if isinstance(data, list):
        return len(data) == 0
    if isinstance(data, dict):
        # Pomijamy znane pola meta (scraped_at, generated, schema_version),
        # potem sprawdzamy czy cokolwiek konkretnego pozostało.
        meta_keys = {"scraped_at", "generated", "schema_version", "kadencja", "id", "label"}
        for k, v in data.items():
            if k in meta_keys:
                continue
            if isinstance(v, (list, dict)):
                if v:
                    return False
            elif v not in (None, "", 0, False):
                return False
        return True
    return False


def read_scraped_at_field(data: Any) -> str | None:
    """Spróbuj wyciągnąć pole `scraped_at` z JSON top-level."""
    if isinstance(data, dict):
        v = data.get("scraped_at")
        if isinstance(v, str) and v:
            return v
    return None


def measure_file(path: Path) -> dict[str, Any]:
    """Zwróć metryki świeżości jednego pliku JSON."""
    if not path.is_file():
        return {"available": False}
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
    except OSError:
        return {"available": False}

    data = read_json(path)
    if data is None or is_payload_empty(data):
        return {"available": False, "reason": "empty"}

    scraped_at = read_scraped_at_field(data)
    if scraped_at:
        return {
            "available": True,
            "scraped_at": scraped_at,
            "source": "json_field",
            "mtime": utc_iso(mtime),
            "size_bytes": size,
            "filename": path.name,
        }
    return {
        "available": True,
        "scraped_at": utc_iso(mtime),
        "source": "mtime",
        "mtime": utc_iso(mtime),
        "size_bytes": size,
        "filename": path.name,
    }


def measure_kadencja(docs_dir: Path) -> dict[str, Any]:
    """Najnowszy plik kadencja-*.json (bieżąca kadencja po mtime)."""
    candidates = sorted(docs_dir.glob("kadencja-*.json"))
    if not candidates:
        return {"available": False}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    info = measure_file(latest)
    if info.get("available"):
        info["kadencja_id"] = latest.stem.removeprefix("kadencja-")
    return info


def load_meta_csv(workspace: Path, filename: str) -> dict[str, dict[str, Any]]:
    """Czyta plik meta CSV (cities-meta.csv lub assemblies-meta.csv). Klucz: slug."""
    candidates = [
        workspace / "radoskop" / "data" / filename,
        Path(__file__).resolve().parent.parent / "data" / filename,
    ]
    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                return {row["slug"]: row for row in csv.DictReader(f)}
    return {}


def load_unit_config(unit_dir: Path) -> dict[str, Any]:
    """Czyta config.json jednostki samorządu (miasta lub sejmiku)."""
    cfg = unit_dir / "config.json"
    if not cfg.is_file():
        return {}
    try:
        with cfg.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def discover_units(workspace: Path, subdir: str) -> list[Path]:
    """Lista katalogów jednostek pod radoskop/{subdir}/ (cities lub sejmiki)."""
    base = workspace / "radoskop" / subdir
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "config.json").is_file())


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def build_unit_entry(unit_dir: Path, level: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Zbuduj wpis dla jednej jednostki (miasta lub sejmiku) w manifeście."""
    cfg = load_unit_config(unit_dir)
    docs_dir = unit_dir / "docs"
    sources: dict[str, Any] = {}
    for src in SOURCES:
        if src["kind"] == "kadencja_glob":
            sources[src["id"]] = measure_kadencja(docs_dir)
        elif src["kind"] == "file":
            sources[src["id"]] = measure_file(docs_dir / src["file"])
        else:
            sources[src["id"]] = {"available": False}

    name = cfg.get(level["name_field"]) or meta.get("name") or unit_dir.name.title()
    # Single-level subdomain dla obu poziomów (gdansk.radoskop.pl,
    # mazowieckie.radoskop.pl). Wildcard CNAME *.radoskop.pl pokrywa.
    default_url = f"https://{unit_dir.name}.radoskop.pl"

    entry: dict[str, Any] = {
        "slug": unit_dir.name,
        "level": level["id"],
        "samorzad_type": cfg.get("samorzad_type") or level["samorzad_type"],
        "name": name,
        "url": cfg.get("site_url") or default_url,
        "bip_url": cfg.get("bip_url"),
        "scrape_status": cfg.get("scrape_status", "active"),
        "sources": sources,
    }

    if level["id"] == "city":
        entry["voivodeship"] = meta.get("voivodeship")
        entry["population"] = parse_int(meta.get("population"))
    else:
        entry["voivodeship"] = meta.get("name") or unit_dir.name
        entry["capital"] = meta.get("capital") or cfg.get("capital")
        entry["population"] = parse_int(meta.get("population"))
        entry["councilor_count"] = parse_int(meta.get("councilor_count")) or cfg.get("councilor_count")

    return entry


def build_planned_assemblies(workspace: Path, active_slugs: set[str]) -> list[dict[str, Any]]:
    """Lista 16 województw z meta CSV, oznacza te bez configu jako 'planned'."""
    meta = load_meta_csv(workspace, "assemblies-meta.csv")
    out: list[dict[str, Any]] = []
    for slug, row in meta.items():
        if slug in active_slugs:
            continue
        out.append({
            "slug": slug,
            "level": "assembly",
            "samorzad_type": "wojewodztwo",
            "name": f"Sejmik Województwa {row.get('name_genitive', slug)}".strip(),
            "url": f"https://{slug}.radoskop.pl",
            "bip_url": None,
            "scrape_status": row.get("status", "planned"),
            "voivodeship": row.get("name") or slug,
            "capital": row.get("capital"),
            "population": parse_int(row.get("population")),
            "councilor_count": parse_int(row.get("councilor_count")),
            "sources": {s["id"]: {"available": False} for s in SOURCES},
        })
    return out


def build_manifest(workspace: Path) -> dict[str, Any]:
    units: list[dict[str, Any]] = []
    assembly_active_slugs: set[str] = set()

    for level in LEVELS:
        meta_map = load_meta_csv(workspace, level["meta_csv"])
        for unit_dir in discover_units(workspace, level["subdir"]):
            meta = meta_map.get(unit_dir.name, {})
            entry = build_unit_entry(unit_dir, level, meta)
            units.append(entry)
            if level["id"] == "assembly":
                assembly_active_slugs.add(unit_dir.name)

    # Dorzucamy 13 województw, dla których nie ma jeszcze configu, żeby
    # macierz pokazywała kompletną listę 16 sejmików ze statusem planned.
    units.extend(build_planned_assemblies(workspace, assembly_active_slugs))

    return {
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": 2,
        "levels": [{"id": l["id"], "label": l["label"]} for l in LEVELS],
        "sources": [
            {"id": s["id"], "label": s["label"], "description": s["description"]}
            for s in SOURCES
        ],
        "units": units,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="Korzeń monorepo (zawiera radoskop/ i radoskop-premium/).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Ścieżka pliku wyjściowego. Domyślnie: <workspace>/radoskop/docs/swiezosc/data.json.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    output = (
        Path(args.output).resolve()
        if args.output
        else workspace / "radoskop" / "docs" / "swiezosc" / "data.json"
    )

    manifest = build_manifest(workspace)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    cities = sum(1 for u in manifest["units"] if u["level"] == "city")
    assemblies = sum(1 for u in manifest["units"] if u["level"] == "assembly")
    print(
        f"build_freshness: zapisano {output} "
        f"({cities} miast, {assemblies} sejmików, {len(SOURCES)} źródeł)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
