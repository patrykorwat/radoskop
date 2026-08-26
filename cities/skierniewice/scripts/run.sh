#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Skierniewice (BIP UM bip.um.skierniewice.pl, kategoria "Imienne wykazy głosowań radnych", PDF wykazy głosowań)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[skierniewice] scrape_skierniewice.py"
python3 "$CITY_DIR/scripts/scrape_skierniewice.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/skierniewice}"

echo "[skierniewice] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[skierniewice] OK"
