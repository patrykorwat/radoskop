#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Piekary Śląskie (Tier-2 roster / Nefeni BIP)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[piekary-slaskie] scrape_piekary.py"
python3 "$CITY_DIR/scripts/scrape_piekary.py" --city-dir "$CITY_DIR"

echo "[piekary-slaskie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[piekary-slaskie] OK"
