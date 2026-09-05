#!/usr/bin/env bash
# Radoskop Łaziska Górne — eSesja scraper (old-template laziskagorne.esesja.pl) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[laziska-gorne] scrape_laziska_gorne.py"
python3 "$SCRIPT_DIR/scrape_laziska_gorne.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/laziska-gorne}/html"

echo "[laziska-gorne] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[laziska-gorne] OK"
