#!/usr/bin/env bash
# Radoskop Chełmno — BIP Logonet cat150: per-session attachments = per-vote imienne prints.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[chelmno] scrape_chelmno.py"
python3 "$SCRIPT_DIR/scrape_chelmno.py" "$CITY_DIR"

echo "[chelmno] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[chelmno] OK"
