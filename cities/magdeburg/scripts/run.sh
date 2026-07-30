#!/usr/bin/env bash
# Pipeline scrape SessionNet dla miasta
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREMIUM_DIR="$(cd "$CITY_DIR/../../../radoskop-premium" && pwd)"
SLUG="$(basename "$CITY_DIR")"

cd "$CITY_DIR"

echo "[$SLUG] sessionnet_scraper.py --build-city"
python3 "$PREMIUM_DIR/scripts/adapters/sessionnet_scraper.py" "$SLUG" --build-city

echo "[$SLUG] generate_site.py"
python3 "$PREMIUM_DIR/../radoskop/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[$SLUG] OK"
