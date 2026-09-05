#!/usr/bin/env bash
# Radoskop Biłgoraj — Tier-2 (BIP eZeto): roster + sesje + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[bilgoraj] scrape_bilgoraj.py"
python3 "$SCRIPT_DIR/scrape_bilgoraj.py"

echo "[bilgoraj] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[bilgoraj] OK"
