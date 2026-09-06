#!/usr/bin/env bash
# Radoskop Nowy Tomyśl — Tier-2 scraper (roster BIP Madkom API + sesje hdsystem) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[nowy-tomysl] scrape_nowy_tomysl.py"
python3 "$SCRIPT_DIR/scrape_nowy_tomysl.py" "$CITY_DIR"

echo "[nowy-tomysl] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nowy-tomysl] OK"
