#!/usr/bin/env bash
# Włodawa — scraper + generate_site (wzorzec ryki/zbaszyn)
set -euo pipefail
CITY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$CITY_DIR/../.." && pwd)"
PY="${PYTHON:-python3}"
$PY "$CITY_DIR/scripts/scrape_wlodawa.py" --city-dir "$CITY_DIR"
$PY "$REPO_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"
