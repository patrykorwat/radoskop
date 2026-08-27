#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Strzelcach Opolskich (bip.strzelceopolskie.pl, skanowane PDFy -> OCR imienne)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="$(dirname "$CITY_DIR")/cache/strzelce"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/strzelce}"

cd "$CITY_DIR"

echo "[strzelce] scrape_strzelce.py (OCR cache = $CACHE)"
mkdir -p "$CACHE"
python3 "$CITY_DIR/scripts/scrape_strzelce.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[strzelce] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[strzelce] OK"
