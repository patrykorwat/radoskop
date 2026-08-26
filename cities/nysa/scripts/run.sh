#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Nysie (BIP nysa.bip.net.pl, Sputnik bip.net.pl
# + tekstowe PDF-y "Wyniki/Wykaz głosowań" per sesja; sesja III przez OCR).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[nysa] scrape_nysa.py"
python3 "$CITY_DIR/scripts/scrape_nysa.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --config "$CITY_DIR/config.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/nysa}"

echo "[nysa] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nysa] OK"
