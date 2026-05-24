#!/usr/bin/env bash
# Pipeline scrape Conseil de Paris.
#
# Paryż głosuje à main levée — protokół (compte rendu sommaire, PDF na
# cdn.paris.fr) podaje WYNIK każdej pozycji (adopté/rejeté/retiré + modalité),
# bez liczb i bez rozbicia na osoby/frakcje. Tryb vote_mode=show_of_hands.
#
# scrape_paris.py --scrape:
#   1. odkrywa linki do comptes rendus sommaires ze strony paris.fr,
#   2. parsuje każdy (wynik + modalité + wnioskodawca vœu),
#   3. pisze docs/kadencja-2020-2026.json + docs/data.json + docs/profiles.json.
#
# Rozbicie na grupy (counts) tylko dla scrutins publics z PV intégral —
# wstawiane osobno przez build_faction_vote_from_tableau (vote_mode=faction).
#
# Kontrakt i procedura odblokowania: radoskop-premium/strategia/GLOSOWANIA_FRAKCYJNE.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[paris] scrape_paris.py --scrape"
python3 scripts/scrape_paris.py --scrape "$@"

echo "[paris] OK"
