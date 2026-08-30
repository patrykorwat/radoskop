#!/usr/bin/env bash
# Radoskop Ryki — Tier-2 (roster / "model berliński"): scrape skład+sesje + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[ryki] scrape_ryki.py"
python3 "$SCRIPT_DIR/scrape_ryki.py" "$CITY_DIR"

echo "[ryki] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[ryki] OK"
