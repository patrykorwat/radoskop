#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Głogów Małopolski (eSesja Portal Mieszkańca, base glogowmalopolski)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[glogow-malopolski] scrape_glogow_malopolski.py"
python3 "$CITY_DIR/scripts/scrape_glogow_malopolski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/glogow-malopolski}"

echo "[glogow-malopolski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[glogow-malopolski] OK"
