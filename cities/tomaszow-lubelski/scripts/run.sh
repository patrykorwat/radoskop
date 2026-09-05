#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Tomaszów Lubelski (eSesja tomaszowlubelski.esesja.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[tomaszow-lubelski] scrape_tomaszow_lubelski.py"
python3 "$CITY_DIR/scripts/scrape_tomaszow_lubelski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/tomaszow-lubelski}"

echo "[tomaszow-lubelski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[tomaszow-lubelski] OK"
