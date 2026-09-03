#!/usr/bin/env bash
# Radoskop radomsko — scraper eurzad.finn.pl (WGRM PDF-y imienne) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[radomsko] scrape_radomsko.py"
python3 "$SCRIPT_DIR/scrape_radomsko.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json"

echo "[radomsko] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[radomsko] OK"
