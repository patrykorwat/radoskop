#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Czarnej Białostockiej (BIP bip-umczarnabialostocka.podlaskie.eu,
# sekcja 'Głosowania Rady Miejskiej' -> per-sesyjny PDF/doc 'Wykaz głosowań' imienne)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[czarna-bialostocka] scrape_czarna_bialostocka.py"
python3 "$CITY_DIR/scripts/scrape_czarna_bialostocka.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/czarna-bialostocka}"

echo "[czarna-bialostocka] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[czarna-bialostocka] OK"
