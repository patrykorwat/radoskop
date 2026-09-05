#!/usr/bin/env bash
# Pipeline scrape Rada Miejska Sandomierza (Tier-2 roster: BIP bip.um.sandomierz.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[sandomierz] scrape_sandomierz.py"
python3 "$CITY_DIR/scripts/scrape_sandomierz.py" --city-dir "$CITY_DIR"

echo "[sandomierz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sandomierz] OK"
