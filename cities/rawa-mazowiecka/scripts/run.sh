#!/usr/bin/env bash
# Radoskop Rawa Mazowiecka — BIP serwer-render + per-sesja 'Imienne wykazy głosowania' PDF.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[rawa] scrape_rawa-mazowiecka.py"
python3 "$SCRIPT_DIR/scrape_rawa-mazowiecka.py" "$CITY_DIR"

echo "[rawa] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[rawa] OK"
