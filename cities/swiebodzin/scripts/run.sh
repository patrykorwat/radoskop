#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Świebodzinie (Tier-2 roster, bip.swiebodzin.eu)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[swiebodzin] scrape_swiebodzin.py"
python3 "$CITY_DIR/scripts/scrape_swiebodzin.py"

echo "[swiebodzin] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[swiebodzin] OK"
