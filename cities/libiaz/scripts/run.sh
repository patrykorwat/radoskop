#!/usr/bin/env bash
# Radoskop Libiąż — portal-posiedzenia.pl scraper (imienne) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[libiaz] scrape_libiaz.py"
python3 "$SCRIPT_DIR/scrape_libiaz.py" --city-dir "$CITY_DIR"

echo "[libiaz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[libiaz] OK"
