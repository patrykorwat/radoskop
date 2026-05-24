#!/usr/bin/env python3
"""Scraper Conseil de Paris — tryb frakcyjny.

Rada Paryża głosuje domyślnie "à main levée" (przez podniesienie ręki).
Oficjalny procès-verbal NIE publikuje głosu pojedynczego radnego, tylko
wynik per grupa polityczna ("tableau des votes par groupe"). Dlatego ten
scraper nie buduje named_votes (lista nazwisk), tylko faction_votes —
poprzez wspólną bibliotekę scripts/lib_faction_votes.make_faction_vote.

Status: SZKIELET. Ścieżka pełnego scrapingu portalu opendata.paris.fr /
parsowania PDF tableau par groupe jest w fazie implementacji. Config miasta
(cities/paris/config.json) ma "voting_display": "faction" i komplet grup w
"clubs", więc gdy tylko scraper zacznie zwracać realne liczniki per grupa,
front (template/index.html) renderuje widok frakcyjny bez dalszych zmian.

Tryby:
    --sample   zapisz pojedyncze ILUSTRACYJNE głosowanie do docs/, żeby
               zweryfikować render end-to-end (docs/ jest gitignored, nie
               jest deployowane). Dane przykładowe, nie protokół.
    (docelowo) --scrape  pobierz realne tableaux par groupe z opendata.paris.fr.

Kontrakt danych i reguły: ../../GLOSOWANIA_FRAKCYJNE.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ repo na ścieżce, żeby zaimportować wspólną bibliotekę frakcyjną.
CITY_DIR = Path(__file__).resolve().parents[1]          # cities/paris
REPO_DIR = CITY_DIR.parents[1]                          # radoskop
sys.path.insert(0, str(REPO_DIR / "scripts"))

from lib_faction_votes import make_faction_vote  # noqa: E402


def load_config() -> dict:
    with open(CITY_DIR / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def sample_votes() -> list[dict]:
    """Dwa ilustracyjne głosowania frakcyjne (dane przykładowe).

    Kody grup pasują do config["clubs"], żeby clubColor() dobrał kolory.
    Liczby są poglądowe — służą tylko do weryfikacji renderu, nie są
    odwzorowaniem realnego protokołu Rady Paryża.
    """
    vote_budget = make_faction_vote(
        vote_id="paris_demo_2024_budget",
        session_date="2024-03-19",
        topic="[DÉMO] Budget primitif 2024 de la Ville de Paris",
        faction_tallies={
            "PARIS_EN_COMMUN": {"za": 78, "przeciw": 0, "wstrzymal_sie": 2, "seats": 80},
            "ECOLOGISTES": {"za": 23, "przeciw": 0, "wstrzymal_sie": 0, "seats": 23},
            "COMMUNISTE": {"za": 11, "przeciw": 0, "wstrzymal_sie": 0, "seats": 11},
            "GENERATIONS": {"za": 5, "przeciw": 0, "wstrzymal_sie": 0, "seats": 5},
            "CHANGER_PARIS": {"za": 0, "przeciw": 24, "wstrzymal_sie": 0, "seats": 24},
            "MODEM": {"za": 0, "przeciw": 8, "wstrzymal_sie": 4, "seats": 12},
            "INDEPENDANTS": {"za": 0, "przeciw": 5, "wstrzymal_sie": 1, "seats": 6},
            "NZ": {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 2, "seats": 2},
        },
        session_number="Séance des 19, 20 et 21 mars 2024",
        source_url="https://www.paris.fr/conseil-de-paris",
        result="ADOPTÉ",
    )
    vote_velo = make_faction_vote(
        vote_id="paris_demo_2024_velo",
        session_date="2024-06-04",
        topic="[DÉMO] Plan vélo 2021-2026 : extension des pistes cyclables",
        faction_tallies={
            "PARIS_EN_COMMUN": {"za": 80, "przeciw": 0, "wstrzymal_sie": 0, "seats": 80},
            "ECOLOGISTES": {"za": 23, "przeciw": 0, "wstrzymal_sie": 0, "seats": 23},
            "COMMUNISTE": {"za": 11, "przeciw": 0, "wstrzymal_sie": 0, "seats": 11},
            "GENERATIONS": {"za": 5, "przeciw": 0, "wstrzymal_sie": 0, "seats": 5},
            "CHANGER_PARIS": {"za": 6, "przeciw": 12, "wstrzymal_sie": 6, "seats": 24},
            "MODEM": {"za": 9, "przeciw": 0, "wstrzymal_sie": 3, "seats": 12},
            "INDEPENDANTS": {"za": 6, "przeciw": 0, "wstrzymal_sie": 0, "seats": 6},
            "NZ": {"za": 1, "przeciw": 0, "wstrzymal_sie": 1, "seats": 2},
        },
        session_number="Séance des 4, 5 et 6 juin 2024",
        source_url="https://www.paris.fr/conseil-de-paris",
        result="ADOPTÉ",
    )
    return [vote_budget, vote_velo]


def write_sample(out_dir: Path) -> Path:
    cfg = load_config()
    kid = cfg["kadencja_active"]
    votes = sample_votes()
    payload = {
        "kadencja": kid,
        "generated_by": "scrape_paris.py --sample (DANE PRZYKŁADOWE)",
        "vote_mode": "faction",
        "votes": votes,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"kadencja-{kid}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_file


def main() -> int:
    ap = argparse.ArgumentParser(description="Scraper Conseil de Paris (tryb frakcyjny)")
    ap.add_argument(
        "--sample",
        action="store_true",
        help="zapisz ilustracyjne głosowania do docs/ (weryfikacja renderu)",
    )
    ap.add_argument(
        "--out",
        default=str(CITY_DIR / "docs"),
        help="katalog wyjściowy (domyślnie cities/paris/docs/, gitignored)",
    )
    args = ap.parse_args()

    if args.sample:
        out = write_sample(Path(args.out))
        print(f"Zapisano dane przykładowe: {out}")
        print("UWAGA: dane ilustracyjne (gitignored), nie protokół Rady Paryża.")
        return 0

    print(
        "Pełny scraper opendata.paris.fr nie jest jeszcze zaimplementowany.\n"
        "Użyj --sample, żeby wygenerować dane przykładowe do weryfikacji renderu.\n"
        "Kontrakt: produkuj rekordy przez lib_faction_votes.make_faction_vote.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
