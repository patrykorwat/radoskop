#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Zduńska Wola (BIP bip.zdunskawola.pl, protokoły z sesji z Wyniki imienne inline)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/zdunska-wola}"

cd "$CITY_DIR"

echo "[zdunska-wola] scrape_zdunska_wola.py (work-dir/cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_zdunska_wola.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[zdunska-wola] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[zdunska-wola] OK"
