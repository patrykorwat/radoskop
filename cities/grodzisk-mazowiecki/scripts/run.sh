#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Grodzisku Mazowieckim (bip.grodzisk.pl, imienne głosowania)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[grodzisk-mazowiecki] scrape_grodzisk_mazowiecki.py"
python3 "$CITY_DIR/scripts/scrape_grodzisk_mazowiecki.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --config "$CITY_DIR/config.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/grodzisk-mazowiecki}"

echo "[grodzisk-mazowiecki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[grodzisk-mazowiecki] OK"
