#!/usr/bin/env bash
# Pipeline scrape Rada Miejska w Olkuszu
# BIP Urzędu Miasta i Gminy w Olkuszu (bip.malopolska.pl/umigolkusz, Madkom SPA),
# załączniki "Głosowania z {ROMAN} sesji..." (PDF-skan / DOCX / PDF-tekst), OCR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RADOSKOP_DIR="$(cd "$CITY_DIR/../.." && pwd)"

cd "$CITY_DIR"

echo "[olkusz] scrape_olkusz.py"
python3 "$CITY_DIR/scripts/scrape_olkusz.py" \
  --output "$CITY_DIR/docs/data.json" \
  --profiles "$CITY_DIR/docs/profiles.json" \
  --cache-dir "${RADOSKOP_CACHE_DIR:-/cache/olkusz}"

echo "[olkusz] generate_site.py"
python3 "$RADOSKOP_DIR/scripts/generate_site.py" \
  --config "$CITY_DIR/config.json" \
  --output "$CITY_DIR/docs/"

echo "[olkusz] OK"
