#!/usr/bin/env bash
# Radoskop Łęczyca — API radni.info (imienne głosowania per radny) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[leczyca] scrape_leczyca.py"
python3 "$SCRIPT_DIR/scrape_leczyca.py" --city-dir "$CITY_DIR"

echo "[leczyca] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[leczyca] OK"
