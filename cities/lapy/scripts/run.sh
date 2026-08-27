#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Łapach (BIP podlaskiej platformy bip-umlapy.podlaskie.eu,
# Głosowania -> Kadencja IX: eSesja-imienne text-format PDFy)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/lapy}"

cd "$CITY_DIR"

echo "[lapy] scrape_lapy.py (cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_lapy.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[lapy] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[lapy] OK"
