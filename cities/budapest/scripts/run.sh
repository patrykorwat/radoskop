#!/usr/bin/env bash
# Wrapper pipeline scrape dla Budapesztu (Fővárosi Közgyűlés).
#
# Budapeszt publikuje jegyzőkönyv jako PDF z warstwą tekstową. Na końcu
# protokołu system głosowań drukuje per uchwałę blok "Szavazás eredménye"
# z imienną tabelą Név/Voks/Frakció. scrape_budapest.py pobiera listę sesji
# z einfoszab.budapest.hu, ściąga jegyzőkönyv, robi pdftotext i parsuje
# bloki imienne. Atrybucja per radny jest domyślna (model jak eSesja).
#
# scrape_budapest.py -> docs/kadencja-{id}.json + docs/profiles.json
# Wymaga: poppler-utils (pdftotext).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[budapest] scrape_budapest.py"
python3 scripts/scrape_budapest.py "$@"

echo "[budapest] OK"
