#!/usr/bin/env bash
# Wrapper pipeline scrape Gemeenteraad Den Haag.
#
# Den Haag używa HTML scrapera denhaag.raadsinformatie.nl (NotuBiz).
# Lista sesji z ORI ElasticSearch (ori_session_name=Gemeenteraad),
# głosowania per-radny z HTML stron vergadering (CSS klasy: in_favor/against/abstain/divided).
#
# Używa wspólnego scrape_notubiz.py z cities/amsterdam/scripts/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SHARED_SCRAPER="$(cd "$CITY_DIR/../../amsterdam/scripts" && pwd)/scrape_notubiz.py"

echo "[denhaag] scrape_notubiz.py"
python3 "$SHARED_SCRAPER" \
  --config "$CITY_DIR/config.json" \
  --docs   "$CITY_DIR/docs" \
  --cache  "$CITY_DIR/.cache" \
  "$@"

echo "[denhaag] OK"
