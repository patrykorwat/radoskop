#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Miastku (AlfaTV "System Rada" rada.miastko.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[miastko] scrape_miastko.py"
python3 "$CITY_DIR/scripts/scrape_miastko.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/miastko}"

echo "[miastko] scrape_interpelacje.py"
python3 "$CITY_DIR/scripts/scrape_interpelacje.py" \
  --output "$CITY_DIR/docs/interpelacje.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/miastko}"

echo "[miastko] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[miastko] OK"
