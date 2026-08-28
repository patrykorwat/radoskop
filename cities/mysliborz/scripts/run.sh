#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Myśliborzu (AlfaTV System Rada rada.mysliborz.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[mysliborz] scrape_mysliborz.py"
python3 "$CITY_DIR/scripts/scrape_mysliborz.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json"

echo "[mysliborz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[mysliborz] OK"
