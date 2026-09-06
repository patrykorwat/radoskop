#!/usr/bin/env bash
# Radoskop Garwolin — BIP eBOI 'Protokół z przebiegu głosowania imiennego' PDFy: scrape + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[garwolin] scrape_garwolin.py"
python3 "$SCRIPT_DIR/scrape_garwolin.py" "$CITY_DIR"

echo "[garwolin] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[garwolin] OK"
