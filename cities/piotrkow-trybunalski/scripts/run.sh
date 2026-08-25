#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Piotrków Trybunalski (BIP AkcessNet www.bip.piotrkow.pl,
# kategorie "Wyniki głosowań ... - {rok}", DOCX per głosowanie)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[piotrkow-trybunalski] scrape_piotrkow_trybunalski.py"
python3 "$CITY_DIR/scripts/scrape_piotrkow_trybunalski.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/piotrkow-trybunalski}"

echo "[piotrkow-trybunalski] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[piotrkow-trybunalski] OK"
