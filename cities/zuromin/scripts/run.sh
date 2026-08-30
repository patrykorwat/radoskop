#!/usr/bin/env bash
# Radoskop Żuromin — imienne głosowania + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[zuromin] scrape_zuromin.py"
python3 "$SCRIPT_DIR/scrape_zuromin.py" --city-dir "$CITY_DIR"

echo "[zuromin] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[zuromin] OK"
