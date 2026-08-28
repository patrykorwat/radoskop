#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Węgorzewie (BIP idcom-jst bip.wegorzewo.pl, eSesja-print PDF)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[wegorzewo] scrape_wegorzewo.py"
python3 "$CITY_DIR/scripts/scrape_wegorzewo.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/wegorzewo}"

echo "[wegorzewo] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[wegorzewo] OK"
