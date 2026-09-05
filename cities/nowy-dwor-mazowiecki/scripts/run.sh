#!/usr/bin/env bash
# Radoskop Nowy Dwór Mazowiecki — eSesja scraper (PM-A) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[nowy-dwor-mazowiecki] scrape_nowy_dwor_mazowiecki.py"
python3 "$SCRIPT_DIR/scrape_nowy_dwor_mazowiecki.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/nowy-dwor-mazowiecki}/html"

echo "[nowy-dwor-mazowiecki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nowy-dwor-mazowiecki] OK"
