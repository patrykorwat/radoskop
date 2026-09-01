#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Skarszewach (Tier-2: BIP bip.skarszewy.pl Madkom SPA API)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[skarszewy] scrape_skarszewy.py"
python3 "$CITY_DIR/scripts/scrape_skarszewy.py" "$CITY_DIR"

echo "[skarszewy] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[skarszewy] OK"
