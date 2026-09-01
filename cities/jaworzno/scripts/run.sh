#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Jaworznie (BIP bip.jaworzno.pl Madkom BIP v2, menu 20227, per-session eSesja-print PDFs)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[jaworzno] scrape_jaworzno.py"
python3 "$CITY_DIR/scripts/scrape_jaworzno.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/jaworzno}"

echo "[jaworzno] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[jaworzno] OK"
