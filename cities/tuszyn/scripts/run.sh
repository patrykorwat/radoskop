#!/usr/bin/env bash
# Radoskop tuszyn — Tier-2 roster+sessions scraper (model berliński, brak głosowań imiennych).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[tuszyn] scrape_tuszyn.py"
python3 "$SCRIPT_DIR/scrape_tuszyn.py" --city-dir "$CITY_DIR"

echo "[tuszyn] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[tuszyn] OK"
