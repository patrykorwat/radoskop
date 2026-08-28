#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Sędziszowie Małopolskim (BIP bip.sedziszow.pl,
# "Protokoły głosowań" /10176 -> per-session text-format vote-protocol PDFs)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/sedziszow-malopolski}"

cd "$CITY_DIR"

echo "[sedziszow-malopolski] scrape_sedziszow_malopolski.py (work-dir/cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_sedziszow_malopolski.py" \
  --city-dir "$CITY_DIR" \
  --work-dir "$CACHE" \
  --cache-dir "$CACHE"

echo "[sedziszow-malopolski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sedziszow-malopolski] OK"
