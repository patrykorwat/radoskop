#!/usr/bin/env bash
# Radoskop Pułtusk — Tier-2 (roster / "model berliński"): scrape skład + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[pultusk] scrape_pultusk.py"
python3 "$SCRIPT_DIR/scrape_pultusk.py" "$CITY_DIR"

echo "[pultusk] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[pultusk] OK"
