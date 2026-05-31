#!/usr/bin/env bash
# Pipeline scrape dla Dніпра (standard KMU 835 / CKAN 5-tabelowy).
#
# Kolejność:
# 1. scrape_kluby.py   → aktualizuje config.json.club_assignments z deputies CSV
# 2. scrape_ckan_ua.py → docs/kadencja-{id}.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[dnipro] [1/2] scrape_kluby.py"
python3 scripts/scrape_kluby.py || echo "[dnipro] WARN: scrape_kluby failed, kontynuuję"

echo "[dnipro] [2/2] scrape_ckan_ua.py"
python3 scripts/scrape_ckan_ua.py "$@"

echo "[dnipro] OK"
