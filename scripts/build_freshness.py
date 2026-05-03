#!/usr/bin/env python3
"""
Buduje manifest świeżości danych dla apex page Radoskopu.

Skanuje `radoskop/cities/{slug}/docs/` w poszukiwaniu plików JSON
(kadencja-*.json, interpelacje.json, aktualnosci.json, budget.json,
profiles.json, data.json), dla każdego pliku ustala znacznik czasu
z pola `scraped_at` (jeśli istnieje) albo z mtime pliku jako fallback,
i zapisuje zbiorczy manifest do `radoskop/docs/swiezosc/data.json`.

Strona /swiezosc/index.html fetchuje ten manifest i renderuje macierz
miasto × źródło danych.

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


# Lista miast z radoskop/data/cities-meta.csv jest źródłem prawdy.
# Tu definiujemy źródła danych do pokazania w macierzy.
SOURCES: list[dict[str, Any]] = [
    {
        "id": "kadencja",
        "label": "Głosowania",
        "description": "Sesje, głosowania, frekwencja, kadencja {id}",
        # Bierzemy najnowszy plik kadencja-*.json (kadencja bieżąca).
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


def utc_iso(ts: float) -> str:
    """Zamień POSIX timestamp na ISO 8601 w UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_scraped_at_field(path: Path) -> str | None:
    """Spróbuj wyciągnąć pole `scraped_at` z JSON top-level."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
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

    scraped_at = read_scraped_at_field(path)
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
    """Najnowszy plik kadencja-*.json (najwyższy zakres lat lub najnowszy mtime)."""
    candidates = sorted(docs_dir.glob("kadencja-*.json"))
    if not candidates:
        return {"available": False}
    # Wybieramy plik z najnowszym mtime (bieżąca kadencja jest aktywnie
    # aktualizowana, więc to dobry wybór).
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    info = measure_file(latest)
    if info.get("available"):
        # Wyciągnij id kadencji z nazwy pliku, np. kadencja-2024-2029.json -> 2024-2029.
        info["kadencja_id"] = latest.stem.removeprefix("kadencja-")
    return info


def load_cities_meta(workspace: Path) -> dict[str, dict[str, Any]]:
    """Czyta cities-meta.csv. Klucz to slug."""
    candidates = [
        workspace / "radoskop" / "data" / "cities-meta.csv",
        Path(__file__).resolve().parent.parent / "data" / "cities-meta.csv",
    ]
    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                return {row["slug"]: row for row in csv.DictReader(f)}
    return {}


def load_city_config(city_dir: Path) -> dict[str, Any]:
    """Czyta config.json miasta (tytuł, URL, BIP)."""
    cfg = city_dir / "config.json"
    if not cfg.is_file():
        return {}
    try:
        with cfg.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def discover_cities(workspace: Path) -> list[Path]:
    """Lista katalogów miast pod radoskop/cities/."""
    base = workspace / "radoskop" / "cities"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / "config.json").is_file())


def build_city_entry(city_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Zbuduj wpis dla jednego miasta w manifeście."""
    cfg = load_city_config(city_dir)
    docs_dir = city_dir / "docs"
    sources: dict[str, Any] = {}
    for src in SOURCES:
        if src["kind"] == "kadencja_glob":
            sources[src["id"]] = measure_kadencja(docs_dir)
        elif src["kind"] == "file":
            sources[src["id"]] = measure_file(docs_dir / src["file"])
        else:
            sources[src["id"]] = {"available": False}

    return {
        "slug": city_dir.name,
        "name": cfg.get("city_name") or city_dir.name.title(),
        "url": cfg.get("site_url") or f"https://{city_dir.name}.radoskop.pl",
        "bip_url": cfg.get("bip_url"),
        "voivodeship": meta.get("voivodeship"),
        "population": int(meta["population"]) if meta.get("population", "").isdigit() else None,
        "sources": sources,
    }


def build_manifest(workspace: Path) -> dict[str, Any]:
    cities_meta = load_cities_meta(workspace)
    cities = []
    for city_dir in discover_cities(workspace):
        meta = cities_meta.get(city_dir.name, {})
        cities.append(build_city_entry(city_dir, meta))

    return {
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": 1,
        "sources": [
            {"id": s["id"], "label": s["label"], "description": s["description"]}
            for s in SOURCES
        ],
        "cities": cities,
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

    cities = manifest["cities"]
    print(f"build_freshness: zapisano {output} ({len(cities)} miast, {len(SOURCES)} źródeł)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
