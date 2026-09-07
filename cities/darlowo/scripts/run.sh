#!/usr/bin/env bash
# Radoskop darlowo — Tier-2 scraper (portal WP REST: sklad + sesje) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[darlowo] scrape_darlowo.py"
python3 "$SCRIPT_DIR/scrape_darlowo.py" "$CITY_DIR"

echo "[darlowo] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[darlowo] OK"
