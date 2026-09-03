#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Malborka (BIP bip.malbork.pl Madkom API, raporty Deputy per-glosowanie)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[malbork] scrape_malbork.py"
python3 "$CITY_DIR/scripts/scrape_malbork.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/malbork}"

echo "[malbork] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[malbork] OK"
