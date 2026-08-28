#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Parczewie (Wrota Lubelszczyzny BIP umparczew.bip.lubelskie.pl
# -> tekstowe PDF-y raport_z_glosowan z imiennymi glowosowaniami)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/parczew}"

cd "$CITY_DIR"

echo "[parczew] scrape_parczew.py (cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_parczew.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[parczew] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[parczew] OK"
