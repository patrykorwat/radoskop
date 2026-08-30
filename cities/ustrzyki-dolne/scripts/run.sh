#!/usr/bin/env bash
# Radoskop Ustrzyki Dolne — Tier-2 (roster / "model berliński"): scrape skład+sesje + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[ustrzyki-dolne] scrape_ustrzyki_dolne.py"
python3 "$SCRIPT_DIR/scrape_ustrzyki_dolne.py" "$CITY_DIR"

echo "[ustrzyki-dolne] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[ustrzyki-dolne] OK"
