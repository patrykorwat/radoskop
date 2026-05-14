#!/usr/bin/env bash
# Wrapper pipeline scrape per kadencja dla Wilna.
#
# Kolejność kroków:
# 1. scrape_kluby.py        → uzupełnia config.json.club_assignments
# 2. scrape_balsavimai.py   → docs/kadencja-{id}.json (taryba publicznie)
#                             + .cache/komitety_raw.json (komitety do premium)
#
# Build_metrics i build_profiles uruchamia generic radoskop scrape_all.sh
# z radoskop-premium pipeline. Tutaj pozostaje tylko warstwa scrape.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[vilnius] [1/2] scrape_kluby.py"
python3 scripts/scrape_kluby.py || echo "[vilnius] WARN: kluby scrape failed, continuing"

echo "[vilnius] [2/2] scrape_balsavimai.py"
python3 scripts/scrape_balsavimai.py "$@"

echo "[vilnius] OK"
