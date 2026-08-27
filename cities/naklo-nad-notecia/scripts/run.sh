#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Nakle nad Notecią (BIP bip.gmina-naklo.pl, protokoły z sesji -> imienne wykazy PDF)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/naklo}"

cd "$CITY_DIR"

echo "[naklo] scrape_naklo.py (work-dir/cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_naklo.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[naklo] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[naklo] OK"
