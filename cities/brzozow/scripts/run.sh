#!/usr/bin/env bash
# Radoskop brzozow — imienne głosowania (DSSS Vote per-głosowanie PDF, bip.gov.pl).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[brzozow] scrape_brzozow.py"
python3 "$SCRIPT_DIR/scrape_brzozow.py" --city-dir "$CITY_DIR"

echo "[brzozow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[brzozow] OK"
