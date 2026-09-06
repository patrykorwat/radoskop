#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Zielona Góra (BIP bip.zielonagora.pl /akty/144/, per-uchwała eSesja-print PDF "Wyniki głosowania")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[zielona-gora] scrape_zielona_gora.py"
python3 "$CITY_DIR/scripts/scrape_zielona_gora.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/zielona-gora}"

echo "[zielona-gora] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[zielona-gora] OK"
