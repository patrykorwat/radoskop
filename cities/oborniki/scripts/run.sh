#!/usr/bin/env bash
# Radoskop Oborniki — Madkom BIP API + per-sesja 'Wyniki glosowan' eSesja-print.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[oborniki] scrape_oborniki.py"
python3 "$SCRIPT_DIR/scrape_oborniki.py" "$CITY_DIR"

echo "[oborniki] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[oborniki] OK"
