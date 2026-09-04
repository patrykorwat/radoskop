#!/usr/bin/env bash
# Radoskop Brzeg — scraping glosowan imiennych (BIP SISCO + wydruki DSSS Vote) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[brzeg] scrape_brzeg.py"
python3 "$SCRIPT_DIR/scrape_brzeg.py" "$CITY_DIR"

echo "[brzeg] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[brzeg] OK"
