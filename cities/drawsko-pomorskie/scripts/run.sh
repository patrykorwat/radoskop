#!/usr/bin/env bash
# Radoskop Drawsko Pomorskie — BIP SSDIP scraper (Raport Głosowania PDF) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[drawsko-pomorskie] scrape_drawsko_pomorskie.py"
python3 "$SCRIPT_DIR/scrape_drawsko_pomorskie.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/drawsko-pomorskie}/html"

echo "[drawsko-pomorskie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[drawsko-pomorskie] OK"
