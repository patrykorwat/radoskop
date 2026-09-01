#!/usr/bin/env bash
# Radoskop Stargard — Tier-2 roster (skład+kluby z config, kalendarz sesji z BIP) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[stargard] scrape_stargard.py"
python3 "$SCRIPT_DIR/scrape_stargard.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/stargard}/html"

echo "[stargard] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[stargard] OK"
