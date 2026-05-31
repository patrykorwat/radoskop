#!/usr/bin/env bash
# Pipeline scrape dla Winnicy — szeroki CSV per sesja (opendata.gov.ua).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[vinnytsia] scrape_vinnytsia.py"
python3 scripts/scrape_vinnytsia.py "$@"

echo "[vinnytsia] OK"
