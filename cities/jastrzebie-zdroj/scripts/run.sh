#!/usr/bin/env bash
# Pipeline scrape Rada Miasta Jastrzębie-Zdrój (BIP Logonet bip.jastrzebie.pl, PDF raporty głosowań)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[jastrzebie-zdroj] scrape_jastrzebie_zdroj.py"
python3 "$CITY_DIR/scripts/scrape_jastrzebie_zdroj.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/jastrzebie-zdroj}"

echo "[jastrzebie-zdroj] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[jastrzebie-zdroj] OK"
