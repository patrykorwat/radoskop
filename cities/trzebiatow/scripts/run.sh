#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Trzebiatowie (BIP bip.trzebiatow.pl, Głosowanie imienne PDF per-uchwała)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[trzebiatow] scrape_trzebiatow.py"
python3 "$CITY_DIR/scripts/scrape_trzebiatow.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "$CITY_DIR/docs/.cache"

echo "[trzebiatow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[trzebiatow] OK"
