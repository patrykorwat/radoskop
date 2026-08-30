#!/usr/bin/env bash
# Radoskop chocianow — Tier-2 (roster / "model berliński"): scrape skład + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[chocianow] scrape_chocianow.py"
python3 "$SCRIPT_DIR/scrape_chocianow.py" "$CITY_DIR"

echo "[chocianow] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[chocianow] OK"
