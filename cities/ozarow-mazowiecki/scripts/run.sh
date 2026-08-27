#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Ożarowie Mazowieckim (eSesja ozarowmazowiecki.esesja.pl)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[ozarow-mazowiecki] scrape_ozarow_mazowiecki.py"
python3 "$CITY_DIR/scripts/scrape_ozarow_mazowiecki.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/ozarow-mazowiecki}"

echo "[ozarow-mazowiecki] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[ozarow-mazowiecki] OK"
