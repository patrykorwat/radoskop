#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Łasku (BIP bip.lask.pl, "Protokoły z sesji i wyniki głosowań"
# /4079/ -> per-session eSesja-imienne text-format PDFy "Wyniki głosowań z N sesji")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/lask}"

cd "$CITY_DIR"

echo "[lask] scrape_lask.py (work-dir/cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_lask.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[lask] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[lask] OK"
