#!/usr/bin/env bash
# Radoskop rudnik-nad-sanem — Tier-2 roster+sesje (BIP Joomla bip.rudnik.pl, DSSS Vote bez atrybucji).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

echo "[rudnik-nad-sanem] scrape_rudnik_nad_sanem.py"
python3 "$SCRIPT_DIR/scrape_rudnik_nad_sanem.py" --city-dir "$CITY_DIR"

echo "[rudnik-nad-sanem] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[rudnik-nad-sanem] OK"
