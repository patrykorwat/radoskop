#!/usr/bin/env bash
# Radoskop zarow — Tier-2: sklad rady + kalendarz sesji (BIP MegaBIP + esesja.tv).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[zarow] scrape_zarow.py"
python3 "$SCRIPT_DIR/scrape_zarow.py" --city-dir "$CITY_DIR" --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/zarow}/html"

echo "[zarow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[zarow] OK"
