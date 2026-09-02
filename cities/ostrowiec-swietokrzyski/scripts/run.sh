#!/usr/bin/env bash
# Radoskop Ostrowiec Świętokrzyski — imienne glosowania (BIP DSSS Vote) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[ostrowiec] scrape"
python3 "$SCRIPT_DIR/scrape_ostrowiec-swietokrzyski.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/ostrowiec-swietokrzyski}/html"

echo "[ostrowiec] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[ostrowiec] OK"
