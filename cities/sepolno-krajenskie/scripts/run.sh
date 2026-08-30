#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Sępólnie Krajeńskim (BIP bip.gmina-sepolno.pl, idcom Waw,
# kategoria 'Imienny wykaz głosowań' -> per-sesyjny PDF eSesja PRINT)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[sepolno-krajenskie] scrape_sepolno.py"
python3 "$CITY_DIR/scripts/scrape_sepolno.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/sepolno-krajenskie}"

echo "[sepolno-krajenskie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sepolno-krajenskie] OK"
