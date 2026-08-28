#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Złocieńcu (AlfaTV "System Rada" rada.zlocieniec.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[zlocieniec] scrape_zlocieniec.py"
python3 "$CITY_DIR/scripts/scrape_zlocieniec.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/zlocieniec}"

echo "[zlocieniec] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[zlocieniec] OK"
