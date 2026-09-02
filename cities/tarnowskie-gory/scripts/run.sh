#!/usr/bin/env bash
# Radoskop Tarnowskie Góry — eSesja scraper + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[tarnowskie-gory] scrape"
python3 "$SCRIPT_DIR/scrape_tarnowskie_gory.py" \
  --output docs/data.json \
  --profiles docs/profiles.json \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/tarnowskie-gory}/html"

echo "[tarnowskie-gory] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[tarnowskie-gory] OK"
