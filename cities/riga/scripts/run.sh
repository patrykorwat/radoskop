#!/usr/bin/env bash
# Wrapper pipeline scrape per kadencja dla Rygi.
#
# Ryga publikuje balsošanas protokols jako SKANY (PDF z drukarki Canon).
# Per-radny głos jest niedostępny w machine-readable form (odręczne podpisy).
# Ten scraper zbiera tylko AGREGATY per głosowanie (counts + result + title).
# Frakcje per radny z zewn. mapping data/deputati_2025_2029.json.
#
# scrape_balsojumi.py → docs/kadencja-{id}.json + docs/profiles.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[riga] scrape_balsojumi.py"
python3 scripts/scrape_balsojumi.py "$@"

echo "[riga] OK"
