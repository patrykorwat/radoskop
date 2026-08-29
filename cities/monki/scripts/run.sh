#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Mońkach (skany "Imienny wykaz głosowań" PDF, OCR)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/monki}"

cd "$CITY_DIR"

echo "[monki] scrape_monki.py (cache = $CACHE)"
RADOSKOP_CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_monki.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[monki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[monki] OK"
