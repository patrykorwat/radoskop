#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Szydłowcu (DSSS Vote tekstowy, bip.szydlowiec.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[szydlowiec] scrape_szydlowiec.py"
python3 "$CITY_DIR/scripts/scrape_szydlowiec.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/szydlowiec}"

echo "[szydlowiec] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[szydlowiec] OK"
