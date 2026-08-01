#!/usr/bin/env bash
# Wrapper pipeline scrape dla Szolnoku (Szolnok Megyei Jogú Város Közgyűlése).
#
# Szolnok publikuje jegyzőkönyv jako PDF przez WordPress + Download Monitor.
# PDF-y są chronione przed bezpośrednim pobieraniem (security plugin),
# dlatego scrape_szolnok.py używa Playwright (prawdziwa przeglądarka).
#
# Wymaga:
#   - playwright (pip install playwright)
#   - chromium (python3 -m playwright install chromium)
#   - poppler-utils (pdftotext)
#
# scrape_szolnok.py -> docs/kadencja-{id}.json + docs/profiles.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[szolnok] scrape_szolnok.py"
python3 scripts/scrape_szolnok.py "$@"

echo "[szolnok] OK"
