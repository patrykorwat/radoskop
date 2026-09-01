#!/usr/bin/env bash
# Radoskop Ostrów Wielkopolski — eSesja PM-A scraper (ostrowmiasto.esesja.pl) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[ostrow-wielkopolski] scrape_ostrow_wielkopolski.py"
python3 "$SCRIPT_DIR/scrape_ostrow_wielkopolski.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/ostrow-wielkopolski}/html"

echo "[ostrow-wielkopolski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[ostrow-wielkopolski] OK"
