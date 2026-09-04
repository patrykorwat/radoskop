#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Giżycka (BIP idcom — załączniki RAR/ZIP/DOCX/PDF raporty Wyniki imienne)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[gizycko] scrape_gizycko.py"
python3 "$CITY_DIR/scripts/scrape_gizycko.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/gizycko}"

echo "[gizycko] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[gizycko] OK"
