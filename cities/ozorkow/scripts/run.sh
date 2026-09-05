#!/usr/bin/env bash
# Radoskop Ozorków — Nefeni BIP: per-session 'raport z głosowań' PDFs (imienne lists).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[ozorkow] scrape_ozorkow.py"
python3 "$SCRIPT_DIR/scrape_ozorkow.py" "$CITY_DIR"

echo "[ozorkow] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[ozorkow] OK"
