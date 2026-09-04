#!/usr/bin/env bash
# Radoskop Jasło — scraper archiwum www2.um.jaslo.pl + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[jaslo] scrape_jaslo.py"
python3 "$SCRIPT_DIR/scrape_jaslo.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json"

echo "[jaslo] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[jaslo] OK"
