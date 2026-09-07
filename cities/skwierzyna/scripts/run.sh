#!/usr/bin/env bash
# Radoskop Skwierzyna — Tier-2 roster+sesje scraper + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[skwierzyna] scrape_skwierzyna.py"
python3 "$SCRIPT_DIR/scrape_skwierzyna.py" "$CITY_DIR"

echo "[skwierzyna] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[skwierzyna] OK"
