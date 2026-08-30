#!/usr/bin/env bash
# Radoskop Wolbrom — Tier-2 (roster): scrape skład + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[wolbrom] scrape_wolbrom.py"
python3 "$SCRIPT_DIR/scrape_wolbrom.py" "$CITY_DIR"

echo "[wolbrom] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[wolbrom] OK"
