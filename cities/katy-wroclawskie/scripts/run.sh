#!/usr/bin/env bash
# Radoskop Kąty Wrocławskie — eSesja scraper (PM-A old-compatible) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[katy-wroclawskie] scrape_katy_wroclawskie.py"
python3 "$SCRIPT_DIR/scrape_katy_wroclawskie.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/katy-wroclawskie}/html"

echo "[katy-wroclawskie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[katy-wroclawskie] OK"
