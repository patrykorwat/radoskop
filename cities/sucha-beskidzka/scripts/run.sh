#!/usr/bin/env bash
# Radoskop Sucha Beskidzka — eSesja scraper (PM-A old-compatible, suchabeskidzka.esesja.pl) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[sucha-beskidzka] scrape_sucha_beskidzka.py"
python3 "$SCRIPT_DIR/scrape_sucha_beskidzka.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/sucha-beskidzka}/html"

echo "[sucha-beskidzka] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[sucha-beskidzka] OK"
