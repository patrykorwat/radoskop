#!/usr/bin/env bash
# Radoskop Kudowa-Zdrój — scraper BIP rejestr głosowań + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[kudowa-zdroj] scrape_kudowa_zdroj.py"
python3 "$SCRIPT_DIR/scrape_kudowa_zdroj.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/kudowa-zdroj}/html"

echo "[kudowa-zdroj] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[kudowa-zdroj] OK"
