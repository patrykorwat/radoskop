#!/bin/bash
# Tier-2 runner: sklad + kalendarz sesji, nastepnie generowanie strony.
set -euo pipefail
CITY_DIR="${1:-$(dirname "$(dirname "$0")")}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
python3 "${SCRIPT_DIR}/scrape_sulkowice.py" --city-dir "${CITY_DIR}" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/sulkowice}/bip"
python3 "${REPO_DIR}/scripts/generate_site.py" --config "${CITY_DIR}/config.json" --output "${CITY_DIR}/docs"
