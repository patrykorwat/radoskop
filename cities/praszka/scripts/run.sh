#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Praszce (custom BIP bip.praszka.pl — protokoły z imiennymi)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[praszka] scrape_praszka.py"
python3 "$CITY_DIR/scripts/scrape_praszka.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/praszka}"

echo "[praszka] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[praszka] OK"
