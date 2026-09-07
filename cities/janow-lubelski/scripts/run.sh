#!/usr/bin/env bash
# Radoskop Janów Lubelski — eSesja scraper (old-template, shortened subdomain) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[janow-lubelski] scrape_janow_lubelski.py"
python3 "$SCRIPT_DIR/scrape_janow_lubelski.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/janow-lubelski}/html"

echo "[janow-lubelski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[janow-lubelski] OK"
