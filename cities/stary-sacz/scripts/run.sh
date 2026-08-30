#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Starym Sączu (BIP bip.malopolska.pl, Wrota Małopolski/Madkom,
# kategoria 'Imienne wykazy głosowań radnych' -> per-sesyjny PDF glosowanieSesja<N>)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[stary-sacz] scrape_stary_sacz.py"
python3 "$CITY_DIR/scripts/scrape_stary_sacz.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/stary-sacz}"

echo "[stary-sacz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[stary-sacz] OK"
