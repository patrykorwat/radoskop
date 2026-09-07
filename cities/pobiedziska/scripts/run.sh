#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Gminy Pobiedziska (BIP bip.pobiedziska.pl Madkom SPA API, protokoły PDF -> imienne głosowania eSesja-TEXT)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/pobiedziska}"

cd "$CITY_DIR"

echo "[pobiedziska] scrape_pobiedziska.py (work-dir/cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_pobiedziska.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[pobiedziska] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[pobiedziska] OK"
