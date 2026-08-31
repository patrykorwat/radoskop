#!/usr/bin/env bash
# Pipeline scrape Rada Miasta i Gminy Łosice (Tier-2: BIP gminalosice.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[losice] scrape_losice.py"
python3 "$CITY_DIR/scripts/scrape_losice.py" "$CITY_DIR"

echo "[losice] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[losice] OK"
