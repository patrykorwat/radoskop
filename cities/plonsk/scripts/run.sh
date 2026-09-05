#!/usr/bin/env bash
# Radoskop Płońsk — scraper DOCX 'Wyniki imiennego głosowania' (umplonsk.bip.org.pl) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"
CACHE="${RADOSKOP_CACHE_DIR:-/cache/plonsk}"

echo "[plonsk] scrape_plonsk.py (docx cache = $CACHE)"
mkdir -p "$CACHE"
python3 "$SCRIPT_DIR/scrape_plonsk.py" --city-dir "$CITY_DIR" --cache-dir "$CACHE"

echo "[plonsk] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[plonsk] OK"
