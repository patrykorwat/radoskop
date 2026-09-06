#!/usr/bin/env bash
# Radoskop Ostrzeszów — BIP biuletyn.net per-sesja PDF "Wyniki głosowań" (imienne) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[ostrzeszow] scrape_ostrzeszow.py"
python3 "$SCRIPT_DIR/scrape_ostrzeszow.py" --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/ostrzeszow}/pdf"

echo "[ostrzeszow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[ostrzeszow] OK"
