#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Sokołowie Podlaskim (eSesja sokolowpodlaski.esesja.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[sokolow-podlaski] scrape_sokolow_podlaski.py"
python3 "$CITY_DIR/scripts/scrape_sokolow_podlaski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/sokolow-podlaski}"

echo "[sokolow-podlaski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sokolow-podlaski] OK"
