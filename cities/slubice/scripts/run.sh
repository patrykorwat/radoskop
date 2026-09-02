#!/usr/bin/env bash
# Radoskop Słubice — Tier-2 (BIP Nefeni Next.js): roster + protokoły sesji + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[slubice] scrape_slubice.py"
python3 "$SCRIPT_DIR/scrape_slubice.py"

echo "[slubice] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[slubice] OK"
