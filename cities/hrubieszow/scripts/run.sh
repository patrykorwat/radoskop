#!/usr/bin/env bash
# Radoskop Hrubieszów — BIP lubelskie.pl AJAX + per-sesja 'Imienne wyniki głosowania' PDF.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[hrubieszow] scrape_hrubieszow.py"
python3 "$SCRIPT_DIR/scrape_hrubieszow.py" "$CITY_DIR"

echo "[hrubieszow] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[hrubieszow] OK"
