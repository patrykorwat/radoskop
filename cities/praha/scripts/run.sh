#!/usr/bin/env bash
# Wrapper pipeline scrape per kadencja dla Prahy.
#
# Kolejność kroków:
# 1. scrape_kluby.py     → uzupełnia config.json.club_assignments
# 2. scrape_glosowania.py → docs/kadencja-{id}.json (LKOD CSV)
# 3. scrape_budget.py    → docs/budget.json (LKOD CSV per rok)
#
# Build_metrics i build_profiles uruchamia generic radoskop scrape_all.sh
# z radoskop-premium pipeline. Tutaj pozostaje tylko warstwa scrape.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[praha] [1/3] scrape_kluby.py"
python3 scripts/scrape_kluby.py

echo "[praha] [2/3] scrape_glosowania.py"
python3 scripts/scrape_glosowania.py "$@"

echo "[praha] [3/3] scrape_budget.py"
python3 scripts/scrape_budget.py || echo "[praha] WARN: budget scrape failed, continuing"

echo "[praha] OK"
