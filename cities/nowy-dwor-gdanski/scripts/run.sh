#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Nowym Dworze Gdańskim (eSesja nowydworgdanski.esesja.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/nowy-dwor-gdanski}"

cd "$CITY_DIR"

echo "[nowy-dwor-gdanski] scrape_nowy_dwor_gdanski.py"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_nowy_dwor_gdanski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "$CACHE"

echo "[nowy-dwor-gdanski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nowy-dwor-gdanski] OK"
