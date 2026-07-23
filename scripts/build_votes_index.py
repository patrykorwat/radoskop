#!/usr/bin/env python3
"""Buduje cross-city indeks głosowań dla wyszukiwarki na radoskop.pl.

Czyta `kadencja-*.json` z każdego `radoskop/cities/{slug}/docs/` (monorepo),
pisze skompresowany `radoskop/docs/votes-index.json`. Plik jest gitignored,
deployowany do `s3://radoskop-public/_main/` przez deploy_main_s3.

Format wyjściowy (kompaktowy, jedna tablica na głosowanie):
    [t, c, i, d, z, p, w]
gdzie:
    t = temat głosowania (skrócony do 160 znaków)
    c = slug miasta (np. "gdansk", "warszawa")
    i = id głosowania (używane jako fragment URL w /glosowanie/<id>/)
    d = data sesji (YYYY-MM-DD)
    z = liczba głosów "za"
    p = liczba głosów "przeciw"
    w = liczba głosów "wstrzymał się"

Rozmiar surowy: ~1.6 MB dla 24 miast. Po gzipie: ~250 KB. Plik ładowany lazy
(dopiero przy pierwszym użyciu wyszukiwarki na froncie).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_TOPIC_LEN = 160


def _city_disabled(config_path: Path) -> bool:
    """True jeśli config ma "disabled": true — miasto NIE wchodzi do indeksu.

    Chroni przed wyciekiem miast bez realnego scrapie (np. paris w trybie
    frakcyjnym z danymi przykładowymi, rostock zablokowany) do publicznego
    cross-city indeksu wyszukiwarki.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("disabled"))
    except Exception:
        return False


def discover_city_dirs(workspace: Path) -> list[tuple[str, Path]]:
    """List (slug, city_dir) for every ENABLED city with a config.json.

    Falls back to legacy sibling layout (`radoskop-{slug}/`) only when the
    monorepo layout is absent — keeps dev environments without the migration
    still working. Miasta z "disabled": true są pomijane.
    """
    out: list[tuple[str, Path]] = []
    mono = workspace / "radoskop" / "cities"
    if mono.is_dir():
        for d in sorted(mono.iterdir()):
            if d.is_dir() and (d / "config.json").exists() and not _city_disabled(d / "config.json"):
                out.append((d.name, d))
        return out
    for d in sorted(workspace.iterdir()):
        if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop-premium":
            slug = d.name[len("radoskop-"):]
            if (d / "config.json").exists() and not _city_disabled(d / "config.json"):
                out.append((slug, d))
    return out


def load_city_votes(city_dir: Path) -> list[dict]:
    """Load all votes from any kadencja-*.json file in the city's docs/."""
    docs = city_dir / "docs"
    if not docs.is_dir():
        return []
    votes: list[dict] = []
    for kad_file in sorted(docs.glob("kadencja-*.json")):
        try:
            with kad_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[warn] {kad_file}: {e}", file=sys.stderr)
            continue
        votes.extend(data.get("votes") or [])
    return votes


def normalize_topic(topic: str) -> str:
    if not topic:
        return ""
    collapsed = " ".join(topic.split())
    if len(collapsed) > MAX_TOPIC_LEN:
        return collapsed[: MAX_TOPIC_LEN - 1] + "…"
    return collapsed


def build_index(workspace: Path) -> list[list]:
    index: list[list] = []
    cities = discover_city_dirs(workspace)
    for slug, city_dir in cities:
        votes = load_city_votes(city_dir)
        if not votes:
            continue
        added = 0
        for v in votes:
            topic = normalize_topic(v.get("topic") or "")
            if not topic:
                continue
            counts = v.get("counts") or {}
            za = int(counts.get("za") or 0)
            przeciw = int(counts.get("przeciw") or 0)
            wstrz = int(counts.get("wstrzymal_sie") or 0)
            index.append([
                topic,
                slug,
                v.get("id") or "",
                v.get("session_date") or "",
                za,
                przeciw,
                wstrz,
            ])
            added += 1
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build cross-city votes index")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root containing radoskop/ (default: parent of this script's repo)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path (default: <workspace>/radoskop/docs/votes-index.json)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    workspace = (
        Path(args.workspace).resolve()
        if args.workspace
        else script_dir.parent.parent  # radoskop/scripts → radoskop → workspace
    )
    out_path = (
        Path(args.output).resolve()
        if args.output
        else workspace / "radoskop" / "docs" / "votes-index.json"
    )

    print(f"workspace: {workspace}", file=sys.stderr)
    index = build_index(workspace)
    # Najnowsze głosowania na górze
    index.sort(key=lambda e: (e[3], e[1], e[2]), reverse=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(f"Zapisano {len(index)} głosowań do {out_path} ({size_kb:.1f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
