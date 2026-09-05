#!/usr/bin/env bash
# Radoskop Bielsk Podlaski — eSesja scraper + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[bielsk-podlaski] scrape_bielsk_podlaski.py"
python3 "$SCRIPT_DIR/scrape_bielsk_podlaski.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/bielsk-podlaski}/html"

echo "[bielsk-podlaski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[bielsk-podlaski] OK"
