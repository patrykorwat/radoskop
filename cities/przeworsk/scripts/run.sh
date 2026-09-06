#!/usr/bin/env bash
# Radoskop Przeworsk — BIP bip.info.pl per-uchwała PDF DSSS Vote App: scrape + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[przeworsk] scrape_przeworsk.py"
python3 "$SCRIPT_DIR/scrape_przeworsk.py" --city-dir "$CITY_DIR" --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/przeworsk}/html"

echo "[przeworsk] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[przeworsk] OK"
