#!/usr/bin/env bash
# Radoskop Biała Podlaska — scrape imiennych glosowan z BIP lubelskie + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[biala-podlaska] scrape_biala_podlaska.py"
python3 "$SCRIPT_DIR/scrape_biala_podlaska.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/biala-podlaska}/html"

echo "[biala-podlaska] generate_site.py"
python3 "$CITY_DIR/../../../scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/" >/dev/null 2>&1 && echo "[ok] generate_site" || echo "[warn] generate_site"

echo "[biala-podlaska] OK"
