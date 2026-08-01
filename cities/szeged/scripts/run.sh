#!/usr/bin/env bash
# Wrapper pipeline scrape dla Szegedu (Szeged Megyei Jogú Város Közgyűlése).
#
# Szeged publikuje jegyzőkönyv jako PDF z warstwą tekstową przez system
# TimPortal (eservices.szeged.eu). scrape_szeged.py pobiera listę sesji z
# API hatarozatok_list.php, ściąga jegyzőkönyv PDF, robi pdftotext i parsuje
# bloki "Szavazás eredménye" z imienną tabelą Név/Voks/Frakció.
#
# scrape_szeged.py -> docs/kadencja-{id}.json + docs/profiles.json
# Wymaga: poppler-utils (pdftotext).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[szeged] scrape_szeged.py"
python3 scripts/scrape_szeged.py "$@"

echo "[szeged] OK"
