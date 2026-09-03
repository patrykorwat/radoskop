#!/usr/bin/env bash
# Radoskop Starogard Gdański — eSesja scraper (old-template) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[starogard-gdanski] scrape_starogard_gdanski.py"
python3 "$SCRIPT_DIR/scrape_starogard_gdanski.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/starogard-gdanski}/html"

echo "[starogard-gdanski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[starogard-gdanski] OK"
