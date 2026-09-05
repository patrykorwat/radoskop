#!/usr/bin/env bash
# Radoskop Hajnówka — głosowania imienne z BIP siteor (PDF/ODT 'Wykaz głosowań') + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[hajnowka] scrape_hajnowka.py"
python3 "$SCRIPT_DIR/scrape_hajnowka.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/hajnowka}"

echo "[hajnowka] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[hajnowka] OK"
