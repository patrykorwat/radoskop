#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Głuchołazach (custom BIP bip.glucholazy.pl, inline-HTML protocols)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/glucholazy}"

cd "$CITY_DIR"

echo "[glucholazy] scrape_glucholazy.py (work-dir/cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_glucholazy.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[glucholazy] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[glucholazy] OK"
