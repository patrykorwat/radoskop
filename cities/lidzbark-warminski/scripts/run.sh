#!/usr/bin/env bash
# Radoskop Lidzbark Warmiński — eSesja scraper + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[lidzbark-warminski] scrape_lidzbark_warminski.py"
python3 "$SCRIPT_DIR/scrape_lidzbark_warminski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/opt/data/workspace/radoskoppl/cache/lidzbark-warminski}/html"

echo "[lidzbark-warminski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[lidzbark-warminski] OK"
