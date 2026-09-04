#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Knurowie (BIP knurow.bip.info.pl API, protokoły z sesji -> imienne bloki PDF)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/knurow}"

cd "$CITY_DIR"

echo "[knurow] scrape_knurow.py (cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_knurow.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[knurow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[knurow] OK"
