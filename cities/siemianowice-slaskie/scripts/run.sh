#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Siemianowice Śląskie (BIP Finn.pl bip.msiemianowicesl.finn.pl /bipkod/35184442, PDF wykazy głosowań)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[siemianowice-slaskie] scrape_siemianowice_slaskie.py"
python3 "$CITY_DIR/scripts/scrape_siemianowice_slaskie.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/siemianowice-slaskie}"

echo "[siemianowice-slaskie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[siemianowice-slaskie] OK"
