#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Starachowicach (BIP bip.um.starachowice.pl, per-sesji 'Głosowania imienne' PDF, eSesja text + OCR skanów)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/starachowice}"

cd "$CITY_DIR"

echo "[starachowice] scrape_starachowice.py (cache = $CACHE)"
python3 "$CITY_DIR/scripts/scrape_starachowice.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[starachowice] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[starachowice] OK"
