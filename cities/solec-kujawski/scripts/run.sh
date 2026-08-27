#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Solcu Kujawskim (eSesja soleckujawski.esesja.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[solec-kujawski] scrape_solec_kujawski.py"
python3 "$CITY_DIR/scripts/scrape_solec_kujawski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/solec-kujawski}"

echo "[solec-kujawski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[solec-kujawski] OK"
