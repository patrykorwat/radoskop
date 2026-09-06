#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Tomaszów Mazowiecki (AlfaTV "System Rada" przasnysz-rada2.alfatv2.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[przasnysz] scrape_przasnysz.py"
python3 "$CITY_DIR/scripts/scrape_przasnysz.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/przasnysz}"

echo "[przasnysz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[przasnysz] OK"
