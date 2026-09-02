#!/usr/bin/env bash
# Radoskop Mielec — Tier-2 (model berliński): roster + kalendarz sesji + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[mielec] scrape_mielec.py"
python3 "$SCRIPT_DIR/scrape_mielec.py"

echo "[mielec] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[mielec] OK"
