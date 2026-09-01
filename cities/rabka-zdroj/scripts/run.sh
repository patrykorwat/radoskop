#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Rabce-Zdroju (custom malopolska BIP, Protokoły głosowań PDF)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/rabka-zdroj}"

cd "$CITY_DIR"

echo "[rabka-zdroj] scrape_rabka_zdroj.py (cache = $CACHE)"
CACHE_DIR="$CACHE" python3 "$CITY_DIR/scripts/scrape_rabka_zdroj.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "$CACHE"

echo "[rabka-zdroj] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"
cp "$CITY_DIR/config.json" "$CITY_DIR/docs/config.json"

echo "[rabka-zdroj] OK"
