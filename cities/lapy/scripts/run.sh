#!/bin/bash
set -euo pipefail
CITY_DIR="${1:?usage: run.sh city_dir}"
cd "$(dirname "$0")/../.."
exec .venv/bin/python scripts/scrape_lapy.py --city-dir "$CITY_DIR"
