#!/usr/bin/env bash
# Wrapper pipeline scrape per kadencja dla Pragi.
#
# Kolejność kroków:
# 1. scrape_kluby.py     → uzupełnia config.json.club_assignments
# 2. scrape_glosowania.py → docs/kadencja-{id}.json
#
# Build_metrics i build_profiles uruchamia generic radoskop scrape_all.sh
# z radoskop-premium pipeline. Tutaj pozostaje tylko warstwa scrape.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[praga] [1/2] scrape_kluby.py"
python3 scripts/scrape_kluby.py

echo "[praga] [2/2] scrape_glosowania.py"
python3 scripts/scrape_glosowania.py "$@"

echo "[praga] OK"
