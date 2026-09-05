#!/usr/bin/env bash
# Radoskop Ząbkowice Śląskie — Tier-2 (roster / "model berliński"): scrape skład+sesje + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[zabkowice-slaskie] scrape_zabkowice_slaskie.py"
python3 "$SCRIPT_DIR/scrape_zabkowice_slaskie.py" "$CITY_DIR"

echo "[zabkowice-slaskie] generate_site.py"
python3 "$(cd "$CITY_DIR/../.." && pwd)/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[zabkowice-slaskie] OK"
