#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Środzie Wielkopolskiej (gov.pl BIP bip.umsroda.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[sroda-wielkopolska] scrape_sroda_wielkopolska.py"
python3 "$CITY_DIR/scripts/scrape_sroda_wielkopolska.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/sroda-wielkopolska}"

echo "[sroda-wielkopolska] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sroda-wielkopolska] OK"
