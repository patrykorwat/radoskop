#!/usr/bin/env python3
"""Buduje zagregowany indeks głosowań ze wszystkich miast dla wyszukiwarki na radoskop.pl.

Czyta plik `kadencja-2024-2029.json` z folderu `docs/` każdego miasta i generuje
skompresowany `votes-index.json` umieszczany w `radoskop/docs/` (strona główna).

Format wyjściowy (kompaktowy, jedna tablica na głosowanie aby ograniczyć rozmiar pliku):
    [t, c, i, d, z, p, w]
gdzie:
    t = temat głosowania (skrócony do 160 znaków)
    c = slug miasta (np. "gdansk", "warszawa")
    i = id głosowania (używane jako fragment URL w /glosowanie/<id>/)
    d = data sesji (YYYY-MM-DD)
    z = liczba głosów "za"
    p = liczba głosów "przeciw"
    w = liczba głosów "wstrzymał się"

Rozmiar surowy: ~1.6 MB. Po gzipie: ~250 KB. Plik ładowany jest dopiero przy
pierwszym użyciu wyszukiwarki (lazy load).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CITIES = [
    "bialystok",
    "bydgoszcz",
    "gdansk",
    "gdynia",
    "katowice",
    "krakow",
    "lodz",
    "lublin",
    "poznan",
    "sopot",
    "szczecin",
    "warszawa",
    "wroclaw",
]

MAX_TOPIC_LEN = 160


def load_city_votes(repo_root: Path, city_slug: str) -> list[dict]:
    path = repo_root / f"radoskop-{city_slug}" / "docs" / "kadencja-2024-2029.json"
    if not path.exists():
        print(f"[warn] brak pliku {path}", file=sys.stderr)
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("votes", []) or []


def normalize_topic(topic: str) -> str:
    if not topic:
        return ""
    collapsed = " ".join(topic.split())
    if len(collapsed) > MAX_TOPIC_LEN:
        return collapsed[: MAX_TOPIC_LEN - 1] + "\u2026"
    return collapsed


def build_index(repo_root: Path) -> list[list]:
    index: list[list] = []
    for city in CITIES:
        votes = load_city_votes(repo_root, city)
        for v in votes:
            topic = normalize_topic(v.get("topic") or "")
            if not topic:
                continue
            counts = v.get("counts") or {}
            za = int(counts.get("za") or 0)
            przeciw = int(counts.get("przeciw") or 0)
            wstrz = int(counts.get("wstrzymal_sie") or 0)
            entry = [
                topic,
                city,
                v.get("id") or "",
                v.get("session_date") or "",
                za,
                przeciw,
                wstrz,
            ]
            index.append(entry)
    return index


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent.parent  # gdansk-network/
    out_path = script_dir.parent / "docs" / "votes-index.json"

    index = build_index(repo_root)

    # Najnowsze głosowania na górze (porządkuj desc po dacie).
    index.sort(key=lambda e: (e[3], e[1], e[2]), reverse=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(
        f"Zapisano {len(index)} głosowań do {out_path} ({size_kb:.1f} KB)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
