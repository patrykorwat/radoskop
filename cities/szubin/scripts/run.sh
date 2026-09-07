#!/usr/bin/env bash
# Radoskop Szubin — scraper posiedzenia.pl (playwright) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[szubin] scrape_szubin.py"
python3 "$SCRIPT_DIR/scrape_szubin.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/szubin}/html"

echo "[szubin] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[szubin] OK"
