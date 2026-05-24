#!/usr/bin/env bash
# Wrapper pipeline scrape dla Schwerina (Stadtvertretung Schwerin).
#
# bis.schwerin.de SessionNet (Somacos) v5.5.4. Niederschriften są summary-only,
# namentliche Abstimmungen siedzą w osobnych załącznikach (Anlage) jako skany
# JPEG obrócone o 90/270 stopni, bez warstwy tekstowej, więc scrape_schwerin.py
# robi OCR (tesseract-ocr-deu + pdf2image + rotacja). Pisze docs/data.json oraz
# docs/kadencja-{id}.json, potem build_assembly_metrics (krok "post") składa
# metryki. scrape_schwerin.py liczy --output i --cache względem cwd, dlatego
# cd do CITY_DIR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"
mkdir -p docs

echo "[schwerin] scrape_schwerin.py --output docs/data.json"
python3 scripts/scrape_schwerin.py --output docs/data.json "$@"

echo "[schwerin] OK"
