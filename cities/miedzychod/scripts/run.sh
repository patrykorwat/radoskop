#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Międzychodu (AlfaTV "System Rada" rada.miedzychod.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[miedzychod] scrape_miedzychod.py"
python3 "$CITY_DIR/scripts/scrape_miedzychod.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/miedzychod}"

echo "[miedzychod] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[miedzychod] OK"
