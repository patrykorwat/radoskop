#!/usr/bin/env bash
# Radoskop Łochów — DSSS Vote PDF protocols: scrape imienne votes + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[lochow] scrape_lochow.py"
python3 "$SCRIPT_DIR/scrape_lochow.py" "$CITY_DIR"

echo "[lochow] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[lochow] OK"
