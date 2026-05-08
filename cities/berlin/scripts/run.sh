#!/usr/bin/env bash
# Pipeline scrape Berlin Abgeordnetenhaus.
#
# Berlin nie ma imiennych głosowań (Hammelsprung default), więc używamy
# modelu monitora aktywności: lista deputowanych + stenogramy → ranking
# słów / sesji z wystąpieniem / drucksachen wnioskowanych.
#
# Kolejność:
# 1. scrape_abgeordnete.py — lista 159 deputowanych + fraktion → config.club_assignments
# 2. scrape_sessions.py    — PARDOK XML + 80 PDF Plenarprotokoll → docs/kadencja-{id}.json + data.json + profiles.json
#
# scrape_sessions sam pisze data.json/profiles.json (bez build_assembly_metrics)
# bo Berlin ma swoje aktywność-based metryki, nie głosowania-based.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CITY_DIR"

echo "[berlin] [1/2] scrape_abgeordnete.py"
python3 scripts/scrape_abgeordnete.py

echo "[berlin] [2/2] scrape_sessions.py"
python3 scripts/scrape_sessions.py "$@"

echo "[berlin] OK"
