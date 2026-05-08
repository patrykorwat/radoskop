#!/usr/bin/env python3
"""
Buduje metryki radnych dla sejmiku województwa.

Czyta `kadencja-{id}.json` (sessions[], votes[], councilor_index[]) plus
`config.json` (clubs, club_assignments). Wylicza per-radny: frekwencję,
aktywność, zgodność z klubem, buntów, podział głosów (za/przeciw/wstrz/
brak/nieobecny). Pisze:

  data.json       — top-level z kadencje[] zawierającym sessions, votes,
                    councilors[] (statystyki) i meta. Schema zgodne z miastami.
  profiles.json   — lista profili radnych: {name, slug, kadencje[]} dla
                    routera SPA `/profil/{slug}/`.

Pisany w analogii do `build_metrics.py` + `build_profiles.py` z miast,
ale bez hardcoded klas klubowych (czyta z config.json) i bez integracji
z Wikipedią. Profile mają puste biogramy, tylko nazwę + klub + slug.
Wzbogacanie biogramami można dorobić jako osobny krok.

Użycie:
    python3 build_assembly_metrics.py \\
        --assembly-dir radoskop/assemblies/mazowieckie

Domyślnie czyta config.json + kadencja-*.json z {assembly-dir}/docs/
i pisze data.json + profiles.json tam samo.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Pomocnicze
# ---------------------------------------------------------------------------

ROMAN_TO_ARABIC = {
    "M": 1000, "CM": 900, "D": 500, "CD": 400,
    "C": 100, "XC": 90, "L": 50, "XL": 40,
    "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1,
}


def make_slug(name: str) -> str:
    """Polski slug: 'Adam Kowalski' -> 'adam-kowalski'."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s\-]", "", ascii_only.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "radny"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Liczenie metryk
# ---------------------------------------------------------------------------

def compute_councilor_metrics(
    kadencja: dict[str, Any],
    club_of: dict[str, str],
) -> list[dict[str, Any]]:
    """Per radny: frekwencja, aktywność, zgodność z klubem, buntów."""
    sessions = kadencja.get("sessions", [])
    votes = kadencja.get("votes", [])
    idx = kadencja.get("councilor_index", [])

    if not idx:
        return []

    # Frekwencja: ile sesji obecny vs total sessions.
    sessions_per_name: Counter = Counter()
    for s in sessions:
        for att in s.get("attendees", []):
            sessions_per_name[att] += 1
    total_sessions = max(len(sessions), 1)

    # Statystyki głosów.
    name_to_idx = {n: i for i, n in enumerate(idx)}
    counts = {
        i: {"za": 0, "przeciw": 0, "wstrzymal": 0, "brak": 0, "nieobecny": 0}
        for i in range(len(idx))
    }
    # Vote-by-vote analiza klubowa.
    rebellions: dict[int, list[dict[str, Any]]] = defaultdict(list)

    cat_to_field = {
        "za": "za", "przeciw": "przeciw", "wstrzymal_sie": "wstrzymal",
        "brak_glosu": "brak", "nieobecni": "nieobecny",
    }

    for v in votes:
        named = v.get("named_votes") or {}
        # Jaka decyzja każdego radnego w tym głosowaniu?
        decision_of: dict[int, str] = {}
        for cat, field in cat_to_field.items():
            for i in named.get(cat, []):
                if i < len(idx):
                    counts[i][field] += 1
                    decision_of[i] = cat

        # Większość per klub (bez nieobecni i brak_glosu).
        from collections import Counter as _C
        club_decisions: dict[str, _C] = defaultdict(_C)
        for i, decision in decision_of.items():
            if decision in ("nieobecni", "brak_glosu"):
                continue
            club = club_of.get(idx[i])
            if club:
                club_decisions[club][decision] += 1
        club_majority: dict[str, str] = {}
        for club, dec_count in club_decisions.items():
            if dec_count:
                club_majority[club] = dec_count.most_common(1)[0][0]

        # Buntów: radny głosował przeciwnie niż większość swojego klubu.
        for i, decision in decision_of.items():
            if decision in ("nieobecni", "brak_glosu"):
                continue
            club = club_of.get(idx[i])
            if not club or club not in club_majority:
                continue
            if decision != club_majority[club]:
                rebellions[i].append({
                    "session": v.get("session_date"),
                    "topic": v.get("topic", "")[:200],
                    "their_vote": decision,
                    "club_majority": club_majority[club],
                })

    total_votes = max(len(votes), 1)
    out: list[dict[str, Any]] = []
    for i, name in enumerate(idx):
        c = counts[i]
        # Frekwencja: % sesji obecny.
        frekwencja = round(sessions_per_name.get(name, 0) / total_sessions * 100, 1)
        # Aktywność: % głosowań aktywnie głosował (za + przeciw + wstrzymał).
        active = c["za"] + c["przeciw"] + c["wstrzymal"]
        aktywnosc = round(active / total_votes * 100, 1)
        # Zgodność z klubem: spośród aktywnych głosów ile zgadzało się z większością.
        rebs = rebellions.get(i, [])
        if active > 0:
            agreed = active - len(rebs)
            zgodnosc = round(agreed / active * 100, 1) if active else 0.0
        else:
            zgodnosc = 0.0

        out.append({
            "name": name,
            "slug": make_slug(name),
            "club": club_of.get(name, "NZ"),
            "frekwencja": frekwencja,
            "aktywnosc": aktywnosc,
            "zgodnosc_z_klubem": zgodnosc,
            "votes_za": c["za"],
            "votes_przeciw": c["przeciw"],
            "votes_wstrzymal": c["wstrzymal"],
            "votes_brak": c["brak"],
            "votes_nieobecny": c["nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": len(rebs),
            "rebellions": rebs,
        })
    return out


def compute_similarity_pairs(
    kadencja: dict[str, Any],
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Top/bottom N par radnych po zgodności decyzji w głosowaniach.

    Tylko głosowania w których obaj głosowali aktywnie (nie nieobecny/brak).
    """
    idx = kadencja.get("councilor_index", [])
    votes = kadencja.get("votes", [])
    if len(idx) < 2 or not votes:
        return [], []

    # Per-radny: dict {vote_id -> decision}.
    per_radny: list[dict[int, str]] = [dict() for _ in idx]
    for v_idx, v in enumerate(votes):
        named = v.get("named_votes") or {}
        for cat, field in (
            ("za", "za"), ("przeciw", "przeciw"),
            ("wstrzymal_sie", "wstrzymal_sie"),
            ("brak_glosu", "brak"), ("nieobecni", "nieobecny"),
        ):
            for i in named.get(cat, []):
                if i < len(idx):
                    per_radny[i][v_idx] = field

    pairs: list[tuple[float, str, str]] = []
    n_radnych = len(idx)
    # Optymalizacja: tylko aktywni radni (>=10 głosowań aktywnie).
    active_mask = [
        sum(1 for d in per_radny[i].values() if d in ("za", "przeciw", "wstrzymal_sie")) >= 10
        for i in range(n_radnych)
    ]

    for i, j in combinations(range(n_radnych), 2):
        if not (active_mask[i] and active_mask[j]):
            continue
        a, b = per_radny[i], per_radny[j]
        common = set(a) & set(b)
        active_common = [
            v_idx for v_idx in common
            if a[v_idx] in ("za", "przeciw", "wstrzymal_sie")
            and b[v_idx] in ("za", "przeciw", "wstrzymal_sie")
        ]
        if len(active_common) < 20:
            continue
        agree = sum(1 for v_idx in active_common if a[v_idx] == b[v_idx])
        score = round(agree / len(active_common) * 100, 1)
        pairs.append((score, idx[i], idx[j]))

    pairs.sort(reverse=True)
    top = [{"a": a, "b": b, "score": s} for s, a, b in pairs[:top_n]]
    bottom = [{"a": a, "b": b, "score": s} for s, a, b in pairs[-top_n:]]
    return top, bottom


# ---------------------------------------------------------------------------
# Build wszystkich plików
# ---------------------------------------------------------------------------

def build_metrics(assembly_dir: Path) -> dict[str, Path]:
    docs = assembly_dir / "docs"
    config = load_json(assembly_dir / "config.json")
    club_of = config.get("club_assignments", {})

    kadencja_files = sorted(docs.glob("kadencja-*.json"))
    if not kadencja_files:
        raise SystemExit(f"brak kadencja-*.json w {docs}")

    kadencje: list[dict[str, Any]] = []
    default_kid: str | None = config.get("kadencja_active")

    profiles_by_slug: dict[str, dict[str, Any]] = {}

    for kad_path in kadencja_files:
        kad = load_json(kad_path)
        kid = kad.get("id") or kad_path.stem.removeprefix("kadencja-")
        label = kad.get("label", f"Kadencja {kid}")

        councilors = compute_councilor_metrics(kad, club_of)
        sim_top, sim_bottom = compute_similarity_pairs(kad)

        # Wzbogacaj kadencję i ZAPISUJ Z POWROTEM (ze świeżymi statystykami).
        kad_full = {
            **kad,
            "id": kid,
            "label": label,
            "councilors": councilors,
            "total_councilors": len(councilors),
            "similarity_top": sim_top,
            "similarity_bottom": sim_bottom,
            "scraped_at": kad.get("scraped_at") or now_iso(),
        }
        write_json(kad_path, kad_full)

        # Lite kadencja do data.json (bez votes — duże).
        kadencje.append({
            "id": kid,
            "label": label,
            "sessions": kad_full.get("sessions", []),
            "total_sessions": kad_full.get("total_sessions", len(kad_full.get("sessions", []))),
            "total_votes": kad_full.get("total_votes", len(kad_full.get("votes", []))),
            "total_councilors": len(councilors),
            "councilors": councilors,
        })

        if default_kid is None:
            default_kid = kid

        # Buduj profile per radny (slug → kadencje[]). SPA template
        # oczekuje pełnego zestawu pól (club_full, okręg, roles, komisje,
        # notes, mid_term, former, votes_za/przeciw/wstrzymal/brak/nieobecny,
        # has_voting_data, has_activity_data). Bez nich profile renderują
        # się pusto, bo template robi defensywne checks na kd.X.
        clubs_meta = config.get("clubs", {}) or {}
        for c in councilors:
            slug = c["slug"]
            entry = profiles_by_slug.setdefault(slug, {
                "name": c["name"], "slug": slug, "kadencje": {},
            })
            club_key = c.get("club") or ""
            club_full = clubs_meta.get(club_key, {}).get("name") or club_key
            entry["kadencje"][kid] = {
                "club": club_key,
                "club_full": club_full,
                "okręg": None,
                "okręg_dzielnice": None,
                "roles": [],
                "komisje": [],
                "notes": "",
                "mid_term": False,
                "former": False,
                "frekwencja": c["frekwencja"],
                "aktywnosc": c["aktywnosc"],
                "zgodnosc_z_klubem": c["zgodnosc_z_klubem"],
                "votes_za": c.get("votes_za", 0),
                "votes_przeciw": c.get("votes_przeciw", 0),
                "votes_wstrzymal": c.get("votes_wstrzymal", 0),
                "votes_brak": c.get("votes_brak", 0),
                "votes_nieobecny": c.get("votes_nieobecny", 0),
                "votes_total": c["votes_total"],
                "rebellion_count": c["rebellion_count"],
                "rebellions": c.get("rebellions", []),
                "has_voting_data": c["votes_total"] > 0,
                "has_activity_data": False,
            }

    # data.json (top-level): kadencje + default_kadencja + scraped_at.
    data_payload = {
        "scraped_at": now_iso(),
        "generated": True,
        "default_kadencja": default_kid,
        "kadencje": kadencje,
    }
    data_path = docs / "data.json"
    write_json(data_path, data_payload)

    # profiles.json: lista profili.
    profiles = sorted(profiles_by_slug.values(), key=lambda p: p["name"].lower())
    profiles_path = docs / "profiles.json"
    write_json(profiles_path, {
        "scraped_at": now_iso(),
        "profiles": profiles,
        "total": len(profiles),
    })

    return {
        "data": data_path,
        "profiles": profiles_path,
        "kadencje": kadencja_files[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assembly-dir", required=True,
        help="Katalog sejmiku (np. radoskop/assemblies/mazowieckie/).",
    )
    args = parser.parse_args()

    assembly_dir = Path(args.assembly_dir).resolve()
    if not (assembly_dir / "config.json").is_file():
        print(f"ERROR: {assembly_dir}/config.json nie istnieje", file=sys.stderr)
        return 1

    paths = build_metrics(assembly_dir)
    print(f"Zapisano:")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
