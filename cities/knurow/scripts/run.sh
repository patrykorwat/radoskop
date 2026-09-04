#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Knurowie (protokoły PDF z Wyniki imienne, BIP Szafr knurow.bip.info.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[knurow] scrape_knurow.py"
python3 "$CITY_DIR/scripts/scrape_knurow.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/knurow}"

echo "[knurow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[knurow] OK"
