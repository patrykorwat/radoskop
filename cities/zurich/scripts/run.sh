#!/usr/bin/env bash
# Wrapper pipeline scrape dla Zurychu (Gemeinderat Zürich).
#
# Zurych publikuje dane o głosowaniach imiennych przez PARIS API
# (Parlamentsinformationssystem) w formacie XML.
# scrape_zurich.py ściąga dane z API i buduje kadencja-*.json + profiles.json.
#
# Wymaga: tylko Python (standard library, żadnych zewn. tooli).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[zurich] scrape_zurich.py"
python3 scripts/scrape_zurich.py "$@"

echo "[zurich] OK"
