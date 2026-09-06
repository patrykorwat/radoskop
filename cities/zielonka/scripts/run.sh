#!/usr/bin/env bash
# Radoskop Zielonka — Madkom BIP API + per-sesja 'Protokoły z głosowań' PDF OCR.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[zielonka] scrape_zielonka.py"
python3 "$SCRIPT_DIR/scrape_zielonka.py" "$CITY_DIR"

echo "[zielonka] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[zielonka] OK"
