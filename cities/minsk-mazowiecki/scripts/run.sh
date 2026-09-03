#!/usr/bin/env bash
# Radoskop Mińsk Mazowiecki — eSesja scraper (PM-A old-compatible) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[minsk-mazowiecki] scrape_minsk_mazowiecki.py"
python3 "$SCRIPT_DIR/scrape_minsk_mazowiecki.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/minsk-mazowiecki}/html"

echo "[minsk-mazowiecki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[minsk-mazowiecki] OK"
