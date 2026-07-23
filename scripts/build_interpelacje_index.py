#!/usr/bin/env python3
"""Buduje cross-city indeks interpelacji i zapytań radnych.

Czyta `interpelacje.json` z każdego `radoskop/cities/{slug}/docs/`. Pisze
`radoskop/docs/interpelacje-index.json`. Gitignored, deployowany do
`s3://radoskop-public/_main/` przez deploy_main_s3, fetchowany przez
wyszukiwarkę aktywności na radoskop.pl.

Per-city `interpelacje.json` ma jeden z dwóch kształtów:
  1. flat list of dicts: `[{cri, ezd, data_wplywu, radny, przedmiot, typ, ...}, ...]`
  2. obiekt: `{"items": [...]}`
Oba obsłużone.

Format wpisu indeksu (kompaktowy):
    [t, c, r, d, k, i, rs]
gdzie:
    t  = przedmiot interpelacji/zapytania (skrócony do 200 znaków)
    c  = slug miasta
    r  = nazwisko radnego (lub "Lastname +" gdy wielu autorów)
    d  = data wpływu (YYYY-MM-DD)
    k  = typ: "i" interpelacja, "z" zapytanie
    i  = cri (id interpelacji w BIP-ie miasta)
    rs = slug radnego dla URL profilu (lub "" gdy wielu autorów / brak profilu)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_TOPIC_LEN = 200


def discover_city_dirs(workspace: Path) -> list[tuple[str, Path]]:
    """List (slug, city_dir) for every city in monorepo (or sibling fallback)."""
    out: list[tuple[str, Path]] = []
    mono = workspace / "radoskop" / "cities"
    if mono.is_dir():
        for d in sorted(mono.iterdir()):
            if d.is_dir() and (d / "config.json").exists():
                out.append((d.name, d))
        return out
    for d in sorted(workspace.iterdir()):
        if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop-premium":
            slug = d.name[len("radoskop-"):]
            if (d / "config.json").exists():
                out.append((slug, d))
    return out


def load_interpelacje(city_dir: Path) -> list[dict]:
    p = city_dir / "docs" / "interpelacje.json"
    if not p.exists():
        return []
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[warn] {p}: {e}", file=sys.stderr)
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items") or []
    return []


def load_profile_slug_map(city_dir: Path) -> dict[str, str]:
    """Map councillor name → URL slug from per-city profiles.json.

    Falls back to deterministic slug if profiles.json is missing.
    """
    p = city_dir / "docs" / "profiles.json"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    profiles = data.get("profiles") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for prof in profiles or []:
        name = (prof.get("name") or "").strip()
        slug = (prof.get("slug") or "").strip()
        if name and slug:
            out[name] = slug
    return out


def fallback_slug(name: str) -> str:
    """Deterministic slug fallback for radnych missing from profiles.json."""
    import unicodedata, re
    s = unicodedata.normalize("NFD", name.lower())
    s = re.sub(r"[̀-ͯ]", "", s)
    s = s.replace("ł", "l").replace("Ł", "L")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def normalize_topic(topic: str) -> str:
    if not topic:
        return ""
    collapsed = " ".join(topic.split())
    if len(collapsed) > MAX_TOPIC_LEN:
        return collapsed[: MAX_TOPIC_LEN - 1] + "…"
    return collapsed


def build_index(workspace: Path) -> list[list]:
    cities = discover_city_dirs(workspace)
    index: list[list] = []
    for slug, city_dir in cities:
        items = load_interpelacje(city_dir)
        if not items:
            continue
        slug_map = load_profile_slug_map(city_dir)
        added = 0
        for it in items:
            topic = normalize_topic(it.get("przedmiot") or it.get("temat") or "")
            if not topic:
                continue
            raw_radny = (it.get("radny") or "").strip()
            multi = "\n" in raw_radny
            primary_name = raw_radny.split("\n", 1)[0].strip() if multi else raw_radny
            radny_label = (primary_name + " +") if multi else primary_name
            # Resolve slug only when there's a single author — multi-author
            # entries don't have a clean target profile.
            radny_slug = ""
            if primary_name and not multi:
                radny_slug = slug_map.get(primary_name) or fallback_slug(primary_name)
            typ_raw = (it.get("typ") or "").lower()
            typ_short = "z" if typ_raw.startswith("zap") or (it.get("cri") or "").startswith("Z") else "i"
            index.append([
                topic,
                slug,
                radny_label,
                it.get("data_wplywu") or "",
                typ_short,
                it.get("cri") or "",
                radny_slug,
            ])
            added += 1
    # Najnowsze na górze
    index.sort(key=lambda e: (e[3], e[1], e[5]), reverse=True)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build cross-city interpelacje/zapytania index")
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
        else workspace / "radoskop" / "docs" / "interpelacje-index.json"
    )

    print(f"workspace: {workspace}", file=sys.stderr)
    index = build_index(workspace)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(
        f"Zapisano {len(index)} interpelacji/zapytań do {out_path} ({size_kb:.1f} KB)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
