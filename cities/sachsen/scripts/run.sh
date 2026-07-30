#!/usr/bin/env bash
# Pipeline scrape Landtag z abgeordnetenwatch.de
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREMIUM_DIR="$(cd "$CITY_DIR/../../../radoskop-premium" && pwd)"

cd "$CITY_DIR"

echo "[$(basename $CITY_DIR)] scrape_aw_landtag.py"
python3 "$PREMIUM_DIR/scripts/scrape_aw_landtag.py" --city-dir "$CITY_DIR"

echo "[$(basename $CITY_DIR) generate_site.py"
python3 "$PREMIUM_DIR/../radoskop/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[$(basename $CITY_DIR)] OK"
