#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Makowie Mazowieckim (DSSS Vote tekstowy, bip.makowmazowiecki.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[makow-mazowiecki] scrape_makow.py"
python3 "$CITY_DIR/scripts/scrape_makow.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/makow-mazowiecki}"

echo "[makow-mazowiecki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[makow-mazowiecki] OK"
