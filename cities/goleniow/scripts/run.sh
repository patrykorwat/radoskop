#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Goleniowie (BIP bip.goleniow.pl, imienne wykazy PDF)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[goleniow] scrape_goleniow.py"
python3 "$CITY_DIR/scripts/scrape_goleniow.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/goleniow}"

echo "[goleniow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[goleniow] OK"
