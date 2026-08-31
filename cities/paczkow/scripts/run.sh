#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Paczkowie (Tier-2: BIP Sputnik paczkow.bip.net.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[paczkow] scrape_paczkow.py"
python3 "$CITY_DIR/scripts/scrape_paczkow.py" "$CITY_DIR"

echo "[paczkow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[paczkow] OK"
