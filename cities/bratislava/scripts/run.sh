#!/usr/bin/env bash
# Wrapper pipeline scrape dla Bratysławy (Mestské zastupiteľstvo).
#
# zastupitelstvo.bratislava.sk to platforma Digitálne zastupiteľstvo z
# JS-renderowanymi tabami. scrape_bratislava.py używa Playwright (chromium
# headless) do renderowania tabu Hlasovanie, bo plain HTTP GET zwraca tylko
# zakładkę Materiały. Wymaga playwright + chromium. Pisze docs/kadencja-{id}.json,
# potem build_assembly_metrics (krok "post" w scrape_all.sh) składa data.json.
#
# Ścieżki w scraperze są względem __file__, więc cd do CITY_DIR jest bezpieczne.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[bratislava] scrape_bratislava.py"
python3 scripts/scrape_bratislava.py "$@"

echo "[bratislava] OK"
