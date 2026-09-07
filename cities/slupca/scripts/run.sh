#!/usr/bin/env bash
# Radoskop slupca — scraper eurzad.finn.pl (ruigr per-głosowanie PDF-y imienne) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[slupca] scrape_slupca.py"
python3 "$SCRIPT_DIR/scrape_slupca.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json"

echo "[slupca] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[slupca] OK"
