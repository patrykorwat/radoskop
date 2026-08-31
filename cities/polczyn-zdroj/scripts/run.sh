#!/usr/bin/env bash
# Radoskop polczyn-zdroj — imienne głosowania (DSSS posiedzenia.pl wykazy PDF na BIP ibip.pl).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[polczyn-zdroj] scrape_polczyn_zdroj.py"
python3 "$SCRIPT_DIR/scrape_polczyn_zdroj.py" --city-dir "$CITY_DIR"

echo "[polczyn-zdroj] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[polczyn-zdroj] OK"
