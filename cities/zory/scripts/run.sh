#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Żory (BIP Nefeni bip.zory.pl / bip-api.zory.pl, per-sesyjne 'raport z głosowań.pdf')
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[zory] scrape_zory.py"
python3 "$CITY_DIR/scripts/scrape_zory.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/zory}"

echo "[zory] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[zory] OK"
