#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Mława (bip.mlawa.pl — Imienne wykazy głosowań Radnych)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[mlawa] scrape_mlawa.py"
python3 "$CITY_DIR/scripts/scrape_mlawa.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --config "$CITY_DIR/config.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/mlawa}"

echo "[mlawa] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[mlawa] OK"
