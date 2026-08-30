#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Oleśnie (custom BIP bip.olesno.pl — imienne TXT w artykułach HTML)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
echo "[olesno] scrape_olesno.py"
python3 "$SCRIPT_DIR/scrape_olesno.py" \
  --city-dir "$CITY_DIR" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-$CITY_DIR/work}"
echo "[olesno] OK"
