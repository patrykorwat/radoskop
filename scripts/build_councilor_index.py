#!/usr/bin/env python3
"""Buduje cross-city indeks radnych dla wyszukiwarki na radoskop.pl.

Czyta `kadencja-*.json` z każdego `radoskop/cities/{slug}/docs/` (monorepo,
fallback na sibling layout). Pisze `radoskop/docs/councilors-index.json`,
gitignored, deployowany do `s3://radoskop-public/_main/` przez
deploy_main_s3.

Wynikowy plik (~60-200 KB w zależności od liczby miast) jest fetchowany
przez index.html przy pierwszym otwarciu wyszukiwarki radnych.

Format wpisu:
    {n: name, c: city_name, u: city_url, s: slug, k: club,
     f: frekwencja, a: aktywnosc, z: zgodnosc_z_klubem}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def discover_city_dirs(workspace: Path) -> list[tuple[str, Path, dict]]:
    """List (slug, city_dir, config) for every city in monorepo or sibling."""
    out: list[tuple[str, Path, dict]] = []
    mono = workspace / "radoskop" / "cities"
    if mono.is_dir():
        for d in sorted(mono.iterdir()):
            cfg = d / "config.json"
            if d.is_dir() and cfg.exists():
                with cfg.open(encoding="utf-8") as f:
                    out.append((d.name, d, json.load(f)))
        return out
    for d in sorted(workspace.iterdir()):
        if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop-premium":
            slug = d.name[len("radoskop-"):]
            cfg = d / "config.json"
            if cfg.exists():
                with cfg.open(encoding="utf-8") as f:
                    out.append((slug, d, json.load(f)))
    return out


# Kanoniczny slugifier wspólny dla całego projektu — patrz lib_slug.py.
from lib_slug import make_slug as slugify  # noqa: E402


def get_slug(name: str, profiles_data: dict | None) -> str:
    if profiles_data:
        for p in profiles_data.get("profiles", []) or []:
            if p.get("name") == name:
                return p.get("slug", slugify(name))
    return slugify(name)


def collect_councilors(city_dir: Path) -> list[dict]:
    """Prefer kadencja-*.json (full councilor stats), fallback to data.json."""
    docs = city_dir / "docs"
    if not docs.is_dir():
        return []
    for kad_file in sorted(docs.glob("kadencja-*.json")):
        try:
            with kad_file.open(encoding="utf-8") as f:
                kad = json.load(f)
        except Exception:
            continue
        cs = kad.get("councilors") or []
        if cs:
            return cs
    data_file = docs / "data.json"
    if data_file.exists():
        try:
            with data_file.open(encoding="utf-8") as f:
                data = json.load(f)
            for k in data.get("kadencje", []) or []:
                if k.get("councilors"):
                    return k["councilors"]
        except Exception:
            pass
    return []


def build_index(workspace: Path) -> list[dict]:
    cities = discover_city_dirs(workspace)
    index: list[dict] = []
    for slug, city_dir, config in cities:
        city_name = config.get("city_name") or slug.capitalize()
        city_url = (config.get("site_url") or f"https://{slug}.radoskop.pl").rstrip("/")

        profiles_data: dict | None = None
        profiles_file = city_dir / "docs" / "profiles.json"
        if profiles_file.exists():
            try:
                with profiles_file.open(encoding="utf-8") as f:
                    profiles_data = json.load(f)
            except Exception:
                pass

        councilors = collect_councilors(city_dir)

        for c in councilors:
            name = c.get("name") or ""
            if not name:
                continue
            index.append({
                "n": name,
                "c": city_name,
                "u": city_url,
                "s": get_slug(name, profiles_data),
                "k": c.get("club", ""),
                "f": round(c.get("frekwencja") or 0, 1),
                "a": round(c.get("aktywnosc") or 0, 1),
                "z": round(c.get("zgodnosc_z_klubem") or 0, 1),
            })

    def sort_key(e: dict) -> tuple[str, str]:
        parts = (e.get("n") or "").split()
        if len(parts) >= 2:
            return (parts[-1].lower(), parts[0].lower())
        return ((e.get("n") or "").lower(), "")

    index.sort(key=sort_key)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build cross-city councilor index")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    workspace = (
        Path(args.workspace).resolve()
        if args.workspace
        else script_dir.parent.parent
    )
    out_path = (
        Path(args.output).resolve()
        if args.output
        else workspace / "radoskop" / "docs" / "councilors-index.json"
    )

    print(f"workspace: {workspace}", file=sys.stderr)
    index = build_index(workspace)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(f"Zapisano {len(index)} radnych do {out_path} ({size_kb:.1f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
