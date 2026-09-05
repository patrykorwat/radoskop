#!/usr/bin/env bash
# Radoskop Konstantynów Łódzki — samorzad.gov.pl 'Sprawozdania z głosowań' PDFy imienne.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[kkl] scrape_konstantynow_lodzki.py"
python3 "$SCRIPT_DIR/scrape_konstantynow_lodzki.py" "$CITY_DIR"

echo "[kkl] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[kkl] OK"
