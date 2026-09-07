#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Nowe Miasto Lubawskie (AlfaTV System Rada rada.umnowemiasto.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[nml] scrape_nowe-miasto-lubawskie.py"
python3 "$CITY_DIR/scripts/scrape_nowe-miasto-lubawskie.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/nowe-miasto-lubawskie}"

echo "[nml] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nml] OK"
