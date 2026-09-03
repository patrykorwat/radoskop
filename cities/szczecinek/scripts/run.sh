#!/usr/bin/env bash
# Radoskop Szczecinek — scraper (BIP uchwaly/attachments, wydruki eSesja per-glosowanie) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[szczecinek] scrape_szczecinek.py"
python3 "$SCRIPT_DIR/scrape_szczecinek.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/szczecinek}/pdfs"

echo "[szczecinek] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[szczecinek] OK"
