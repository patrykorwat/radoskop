#!/usr/bin/env python3
"""
Scraper Dніпровської міської ради — standard KMU 835 (5-tabelowy CSV).

Dane publikowane na:
  https://data.dniprorada.gov.ua (lokalny CKAN 2.8.3)

Dataset głosowań (5 tabel CSV):
  https://data.dniprorada.gov.ua/dataset/385eb385-1d2a-4de5-a95e-23889a25631a
  CKAN internal ID: cd170f44-006b-4889-a496-f045b87fca5f

Dataset radnych (voterUid → imię + frakcja):
  https://data.dniprorada.gov.ua/dataset/1f8b635d-8924-47a0-b655-99227ede84ff
  CKAN internal ID: ed6dab52-ab26-42d4-88f9-843608bdefb5

Schemat 5 tabel:
  convocations  uid / label / dateFrom / dateUntil
  sessions      uid / label / convocationUid / dateFrom / dateUntil
  motions       uid / sessionUid / title / number / date / classification
  voteEvents    uid / motionUid / projectNumber / projectTitle / startDate /
                result / votingFor / votingAgainst / votingAbstain / notVoting /
                absent / textUrl
  vote          uid (=voteEvent.uid) / voterUid / result

Schemat radnych:
  id / votingIdentifier / familyName / name / additionalName /
  factionName / ...

Użycie:
  python3 scrape_ckan_ua.py
  python3 scrape_ckan_ua.py --kadencja-id 2020-2025
  python3 scrape_ckan_ua.py --skip-fetch        # użyj cache
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
RADOSKOP_SCRIPTS = CITY_DIR.parent.parent / "scripts"
sys.path.insert(0, str(RADOSKOP_SCRIPTS))

from lib_ckan_ua import CkanUaClient  # noqa: E402

DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id", help="Konkretna kadencja. Domyślnie wszystkie.")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Użyj cache zamiast pobierać dane.",
    )
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    args.docs.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)

    ckan_base = config["ckan_base"]
    votes_id = config["ckan_votes_id"]
    deputies_id = config.get("ckan_deputies_id")
    city_slug = config.get("slug", "dnipro")

    client = CkanUaClient(
        ckan_base=ckan_base,
        votes_dataset_id=votes_id,
        deputies_dataset_id=deputies_id,
        cache_dir=args.cache,
        skip_fetch=args.skip_fetch,
    )

    config["slug"] = city_slug

    kadencje_to_build = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    # Usuń stare kadencja-*.json których nie ma już w config
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            try:
                old.unlink()
                print(f"[dnipro] removed stale {old.name}", file=sys.stderr)
            except OSError as exc:
                print(f"[dnipro] WARN: cannot remove {old.name}: {exc}", file=sys.stderr)

    for kid in kadencje_to_build:
        print(f"[dnipro] budowanie kadencja-{kid}", file=sys.stderr)
        built = client.build_kadencja(config, kid)

        if built is None or not built.get("votes"):
            print(
                f"[dnipro] skip kadencja-{kid}: 0 głosowań",
                file=sys.stderr,
            )
            continue

        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(built, f, ensure_ascii=False, indent=2)

        print(
            f"[dnipro] napisano {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
