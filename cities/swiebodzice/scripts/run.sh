#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Świebodzicach (Tier-2 roster: BIP bip.swiebodzice.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[swiebodzice] scrape_swiebodzice.py"
python3 "$CITY_DIR/scripts/scrape_swiebodzice.py" --city-dir "$CITY_DIR"

echo "[swiebodzice] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[swiebodzice] OK"
