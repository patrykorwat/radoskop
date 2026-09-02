#!/usr/bin/env bash
# Radoskop Oborniki — Tier-2 (roster+aktywność eSesja PM): scrape + generate_site.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$(cd "$CITY_DIR/../.." && pwd)"
PY="${PYTHON:-python3}"
mkdir -p "$CITY_DIR/docs"
"$PY" "$SCRIPT_DIR/scrape_oborniki.py" "$CITY_DIR"
"$PY" "$REPO_DIR/scripts/generate_site.py" --config "$CITY_DIR/config.json" --output "$CITY_DIR/docs"
echo "[run.sh] oborniki OK"
