#!/usr/bin/env bash
# Radoskop Gubin — głosowania imienne (rejestr aktów BIP, PDF 'wynik głosowania') + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[gubin] scrape_gubin.py"
python3 "$SCRIPT_DIR/scrape_gubin.py" "$CITY_DIR"

echo "[gubin] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[gubin] OK"
