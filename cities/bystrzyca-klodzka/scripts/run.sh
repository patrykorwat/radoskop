#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Bystrzycy Kłodzkiej (eSesja imienne TEXT w protokołach PDF, bip.bystrzycaklodzka.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[bystrzyca-klodzka] scrape_bystrzyca_klodzka.py"
python3 "$CITY_DIR/scripts/scrape_bystrzyca_klodzka.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/bystrzyca-klodzka}"

echo "[bystrzyca-klodzka] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[bystrzyca-klodzka] OK"
