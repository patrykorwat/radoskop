#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Lubsku (BIP bip.lubsko.pl, "Protokoły z głosowań kadencja IX"
# /343/ -> per-session eSesja-imienne text-format PDFy "Protokol_Rada24_posiedzenie_N.pdf")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/lubsko}"

cd "$CITY_DIR"

echo "[lubsko] scrape_lubsko.py (work-dir/cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_lubsko.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[lubsko] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[lubsko] OK"
