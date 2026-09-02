#!/usr/bin/env bash
# Radoskop Stalowa Wola — eSesja PM-A (stalowawola.esesja.pl): scrape + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[stalowa-wola] scrape_stalowa_wola.py"
python3 "$SCRIPT_DIR/scrape_stalowa_wola.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/stalowa-wola}/html"

echo "[stalowa-wola] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[stalowa-wola] OK"
