#!/usr/bin/env bash
# Radoskop Gryfów Śląski — eSesja scraper (slug 'gryfowslaski') + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[gryfow-slaski] scrape_gryfow_slaski.py"
python3 "$SCRIPT_DIR/scrape_gryfow_slaski.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/gryfow-slaski}/html"

echo "[gryfow-slaski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[gryfow-slaski] OK"
