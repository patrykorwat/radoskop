#!/usr/bin/env bash
# Wrapper pipeline scrape dla Klaipėdy.
#
# Klaipėda nie ma osobnego scrapera klubów - frakcje pochodzą z
# config.json.club_assignments (lista oficjalna BIP, patrz
# feedback_club_assignment_current). Tu leci tylko warstwa balsavimai.
#
# scrape_balsavimai.py -> docs/kadencja-{id}.json (assembly-style).
# build_assembly_metrics uruchamia generic radoskop pipeline (post=assembly_metrics).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[klaipeda] scrape_balsavimai.py"
python3 scripts/scrape_balsavimai.py "$@"

echo "[klaipeda] OK"
