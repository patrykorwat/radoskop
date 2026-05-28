#!/usr/bin/env bash
# Pipeline scrape Borgerrepræsentationen København (kk.dk).
#
# Dania w Tier 4 z eu_council_voting_analysis.md: głosowania protokołowane
# pr. parti, nie pr. medlem (model identyczny z Francją). Po dodaniu
# wsparcia dla widoku frakcyjnego (radoskop-premium/strategia/GLOSOWANIA_FRAKCYJNE.md)
# da się obsłużyć Kopenhagę przez vote_mode=faction.
#
# scrape_kk.py --scrape:
#   1. odkrywa wszystkie posiedzenia BR z indeksu kk.dk/dagsordener-og-referater,
#   2. pobiera każdy referat, odkrywa punkty,
#   3. parsuje sekcję Beslutning ('uden afstemning' -> show_of_hands,
#      'For stemte: ... Imod stemte: ...' -> faction),
#   4. pisze docs/kadencja-{id}.json + docs/data.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[copenhagen] scrape_kk.py --scrape"
python3 scripts/scrape_kk.py --scrape "$@"

echo "[copenhagen] OK"
