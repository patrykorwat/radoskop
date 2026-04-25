#!/usr/bin/env python3
"""
Stub scrapera interpelacji Rady Miasta Bielsko-Biała. Zapisuje pustą listę.
"""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump([], f)
    print(f"[Bielsko-Biała] interpelacje stub: empty list written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
