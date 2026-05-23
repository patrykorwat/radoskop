#!/usr/bin/env bash
# Wrapper pipeline scrape Gemeenteraad Amsterdam.
#
# Amsterdam używa HTML scrapera amsterdam.raadsinformatie.nl (NotuBiz).
# Lista sesji z ORI ElasticSearch, głosowania per-radny z HTML stron vergadering.
# Brak osobnych scraperów klubów — party_name_to_slug w config.json.
#
# scrape_notubiz.py → docs/kadencja-{id}.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[amsterdam] scrape_notubiz.py"
python3 scripts/scrape_notubiz.py "$@"

echo "[amsterdam] OK"
