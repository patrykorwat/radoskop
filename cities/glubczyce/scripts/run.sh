#!/usr/bin/env bash
# Radoskop Głubczyce — Tier-2 scraper (roster oświadczenia BIP + sesje z protokołów PDF) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[glubczyce] scrape_glubczyce.py"
python3 "$SCRIPT_DIR/scrape_glubczyce.py" "$CITY_DIR"

echo "[glubczyce] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[glubczyce] OK"
