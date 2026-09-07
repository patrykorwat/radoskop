#!/usr/bin/env bash
# Radoskop pszow — scraper eurzad.finn.pl (PRM6 'Protokół głosowania' PDF-y imienne) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[pszow] scrape_pszow.py"
python3 "$SCRIPT_DIR/scrape_pszow.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json"

echo "[pszow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[pszow] OK"
