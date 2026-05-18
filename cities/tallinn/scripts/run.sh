#!/usr/bin/env bash
# Wrapper pipeline scrape per kadencja dla Tallina.
#
# Tallin używa pojedynczego scrapera bo TEELE API zwraca w jednym przebiegu
# i listę sesji i imienne głosowania i nazwy frakcji per radny. Brak osobnego
# scrape_kluby (Wilno potrzebuje bo data.gov.lt nie ma frakcji w datasecie).
#
# scrape_haaletused.py → docs/kadencja-{id}.json + docs/profiles.json
#
# Build_metrics uruchamia radoskop-premium scrape_all.sh po tym wrapperze,
# składa data.json finalny.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[tallinn] scrape_haaletused.py"
python3 scripts/scrape_haaletused.py "$@"

echo "[tallinn] OK"
