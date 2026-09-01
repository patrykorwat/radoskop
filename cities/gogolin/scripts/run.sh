#!/usr/bin/env bash
# Radoskop gogolin — imienne głosowania (Rada365 wykaz glosowan PDF, bip.gogolin.pl).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[gogolin] scrape_gogolin.py"
python3 "$SCRIPT_DIR/scrape_gogolin.py" --city-dir "$CITY_DIR" --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/gogolin}/html"

echo "[gogolin] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[gogolin] OK"
