#!/usr/bin/env bash
# Pipeline scrape dla Kijowa — JSON ZIPs per sesja (kwartalnie na data.gov.ua).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[kyiv] [1/2] scrape_kluby.py"
python3 scripts/scrape_kluby.py || echo "[kyiv] WARN: scrape_kluby failed, kontynuuję"

echo "[kyiv] [2/2] scrape_kyiv.py"
python3 scripts/scrape_kyiv.py "$@"

echo "[kyiv] OK"
