#!/usr/bin/env bash
# Radoskop Wysokie Mazowieckie — eSesja scraper (PM-A old-compatible, subdomena wysokiemazowieckieum) + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[wysokie-mazowieckie] scrape_wysokie_mazowieckie.py"
python3 "$SCRIPT_DIR/scrape_wysokie_mazowieckie.py" \
  --output "docs/data.json" \
  --profiles "docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/wysokie-mazowieckie}/html"

echo "[wysokie-mazowieckie] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[wysokie-mazowieckie] OK"
