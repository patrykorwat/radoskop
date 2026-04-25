#!/usr/bin/env python3
"""
Scraper sesji i głosowań Rady Miasta Bytom.

Status: STUB (TODO).
eSesja platform — port logic from scrape_bialystok.py.
BIP: https://www.bip.um.bytom.pl/
eSesja: https://bytom.esesja.pl

Konwencja wyjścia: docs/data.json + docs/profiles.json (jak inne miasta).

Stub zapisuje pusty data.json żeby pipeline nie wywalał się — miasto
pojawia się na głównej z populacją + linkiem do BIP, bez danych głosowań.
Scraper zaimplementować na podstawie scrape_bialystok.py (eSesja) lub
custom per BIP.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path


def write_empty(path: Path, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "default_kadencja": "2024-2029",
        "kadencje": [],
        "_status": "scraper_not_implemented",
    } if kind == "data" else []
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper Radoskop Bytom (stub)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"[Bytom] stub scraper, writing empty payloads")
    write_empty(args.output, "data")
    write_empty(args.profiles, "profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
