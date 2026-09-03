#!/usr/bin/env bash
# Radoskop Nowa Sól — portal-posiedzenia.pl scraper + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[nowasol] scrape_nowasol.py"
python3 "$SCRIPT_DIR/scrape_nowasol.py" --city-dir "$CITY_DIR"

echo "[nowasol] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[nowasol] OK"
