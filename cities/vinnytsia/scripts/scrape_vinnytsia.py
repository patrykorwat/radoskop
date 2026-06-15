#!/usr/bin/env python3
"""
Scraper Вінницької міської ради — format szeroki CSV per sesja.

Dane na data.gov.ua / opendata.gov.ua, organizacja vinnytska-miska-rada.
Dataset: 3d8bbfe2-c725-4644-8f86-9a5565bd9f12

Format pliku CSV:
  Separator: średnik (;)
  Kodowanie: Windows-1251 (cp1251) lub UTF-8 z BOM
  Wiersze: jeden wiersz = jedno głosowanie
  Kolumny:
    id          — numer głosowania
    FullAskText — treść pytania do głosowania
    <Surname>   — jedna kolumna per radny (transliterowane nazwisko łacińskie)
    result      — wynik głosowania

  Wartości głosu w Cyrylicy: За / Проти / Утримався / Не голосував / Відсутній
  Wynik: Прийнято / Не прийнято / Відхилено

Nazewnictwo zasobów:
  convocation-8-session-56-date-2025-04-25.csv
  Pattern: convocation-{CONV}-session-{SESS}-date-{DATE}.csv

Uwaga: pliki dostępne też na opendata.gov.ua (mirror) gdy data.gov.ua nie odpowiada.

Użycie:
  python3 scrape_vinnytsia.py
  python3 scrape_vinnytsia.py --kadencja-id 2020-2025
  python3 scrape_vinnytsia.py --skip-fetch
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

RADOSKOP_SCRIPTS = CITY_DIR.parent.parent / "scripts"
sys.path.insert(0, str(RADOSKOP_SCRIPTS))

from lib_ua_http import http_get, ckan_resources_with_cache  # noqa: E402

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Pattern nazwy zasobu → data sesji
SESSION_RES_RE = re.compile(
    r"convocation-(\d+)-session-(\d+)-date-(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

def decode_csv(raw: bytes) -> str:
    """Próbuje UTF-8 z BOM, potem cp1251."""
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("cp1251", errors="replace")


def parse_wide_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parsuje szeroki CSV.

    Zwraca: (councilor_columns, rows)
    councilor_columns: lista nazw kolumn radnych (bez 'id', 'FullAskText', 'result')
    rows: lista wierszy jako dict column→value
    """
    text = decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    if not rows:
        return [], []
    fieldnames = reader.fieldnames or []
    skip = {"id", "FullAskText", "result", ""}
    councilor_cols = [f for f in fieldnames if f not in skip]
    return councilor_cols, rows


def map_vote(token: str, vote_text_map: dict[str, str]) -> str | None:
    t = token.strip()
    if t in vote_text_map:
        return vote_text_map[t]
    # Fuzzy: ignoruj wielkość liter
    tl = t.lower()
    for key, val in vote_text_map.items():
        if key.lower() == tl:
            return val
    return None


def map_result(token: str, result_text_map: dict[str, str]) -> str:
    t = token.strip()
    if t in result_text_map:
        return result_text_map[t]
    return t


def kadencja_for_date(date_str: str, kadencje: dict) -> str | None:
    if not date_str:
        return None
    sorted_kad = sorted(kadencje.items(), key=lambda kv: kv[1].get("start", ""), reverse=True)
    for kid, kdef in sorted_kad:
        if date_str >= kdef.get("start", ""):
            return kid
    return None


def build_kadencja(
    all_sessions: list[dict[str, Any]],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any] | None:
    kadencje = config.get("kadencje", {})
    city_slug = config.get("slug", "vinnytsia")
    vote_text_map = config.get("vote_text_map", {})
    result_text_map = config.get("result_text_map", {})

    relevant = [
        s for s in all_sessions
        if kadencja_for_date(s["date"], kadencje) == kadencja_id
    ]
    if not relevant:
        return None

    # Zbierz wszystkich radnych (z nagłówków kolumn)
    all_cols: set[str] = set()
    for sess in relevant:
        all_cols.update(sess.get("councilor_cols", []))
    councilor_index = sorted(all_cols)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    sessions_out: list[dict[str, Any]] = []
    votes_out: list[dict[str, Any]] = []

    for sess in sorted(relevant, key=lambda s: s["date"]):
        date_str = sess["date"]
        session_no = sess.get("session_no", "")
        rows = sess.get("rows", [])
        cols = sess.get("councilor_cols", [])

        present: set[str] = set()
        sess_votes: list[dict[str, Any]] = []

        for row_no, row in enumerate(rows):
            topic = row.get("FullAskText", "").strip()
            result_raw = row.get("result", "").strip()
            result_cat = map_result(result_raw, result_text_map)

            counts: dict[str, int] = {c: 0 for c in CATEGORIES}
            named_votes_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}

            for col in cols:
                vote_raw = row.get(col, "").strip()
                cat = map_vote(vote_raw, vote_text_map)
                if not cat:
                    continue
                bucket = "nieobecni" if cat == "nieobecny" else cat
                counts[bucket] += 1
                if col in name_to_idx:
                    named_votes_idx[bucket].append(name_to_idx[col])
                if cat not in ("nieobecny",):
                    present.add(col)

            for bucket in named_votes_idx:
                named_votes_idx[bucket].sort()

            vote_id = f"{city_slug}_{date_str}_{session_no}_{row_no + 1}"
            sess_votes.append({
                "id": vote_id,
                "session_date": date_str,
                "session_number": date_str,
                "source_url": "",
                "topic": topic,
                "druk": row.get("id", ""),
                "resolution": "",
                "result": result_cat,
                "result_native": result_raw,
                "counts": counts,
                "named_votes": named_votes_idx,
                "voted_at": "",
            })

        votes_out.extend(sess_votes)
        attendees = sorted(present)
        sessions_out.append({
            "date": date_str,
            "number": date_str,
            "title": f"Сесія {session_no}",
            "vote_count": len(sess_votes),
            "attendee_count": len(attendees),
            "attendees": attendees,
            "source_url": "",
        })

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kadencja_def = kadencje[kadencja_id]

    return {
        "id": kadencja_id,
        "label": kadencja_def.get("label", kadencja_id),
        "scraped_at": scraped_at,
        "sessions": sessions_out,
        "votes": votes_out,
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id")
    parser.add_argument("--skip-fetch", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    args.docs.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)

    dataset_id = config["ckan_votes_dataset_id"]
    ckan_timeout = int(config.get("ckan_timeout", 30))
    cache_index = args.cache / "resources.json"

    resources, stale = ckan_resources_with_cache(
        dataset_id,
        cache_path=cache_index,
        skip_fetch=args.skip_fetch,
        timeout=ckan_timeout,
        label="vinnytsia",
    )
    if resources is None:
        return 1
    if stale:
        print("[vinnytsia] UWAGA: lista zasobów z cache (data.gov.ua odrzuciło żądanie)", file=sys.stderr)

    # Filtruj zasoby sesji
    session_resources = [
        r for r in resources
        if SESSION_RES_RE.search(r.get("name", ""))
        and r.get("format", "").upper() == "CSV"
    ]
    print(f"[vinnytsia] {len(session_resources)} zasobów sesji CSV", file=sys.stderr)

    # Pobierz i sparsuj każdą sesję
    all_sessions: list[dict[str, Any]] = []
    for i, res in enumerate(sorted(session_resources, key=lambda r: r.get("name", "")), 1):
        name = res.get("name", "")
        url = res["url"]
        m = SESSION_RES_RE.search(name)
        date_str = m.group(3) if m else ""
        session_no = m.group(2) if m else ""

        csv_cache = args.cache / f"session_{session_no}.json"
        if args.skip_fetch and csv_cache.exists():
            with open(csv_cache, encoding="utf-8") as f:
                sess_data = json.load(f)
        else:
            print(f"[vinnytsia] [{i}/{len(session_resources)}] sesja {session_no} ({date_str})", file=sys.stderr)
            try:
                raw = http_get(url, timeout=60)
            except RuntimeError as exc:
                print(f"  WARN: skip: {exc}", file=sys.stderr)
                continue
            councilor_cols, rows = parse_wide_csv(raw)
            sess_data = {
                "date": date_str,
                "session_no": session_no,
                "councilor_cols": councilor_cols,
                "rows": rows,
            }
            with open(csv_cache, "w", encoding="utf-8") as f:
                json.dump(sess_data, f, ensure_ascii=False)

        all_sessions.append(sess_data)

    print(f"[vinnytsia] {len(all_sessions)} sesji łącznie", file=sys.stderr)

    kadencje_to_build = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            try:
                old.unlink()
            except OSError:
                pass

    for kid in kadencje_to_build:
        print(f"[vinnytsia] budowanie kadencja-{kid}", file=sys.stderr)
        built = build_kadencja(all_sessions, config, kid)
        if built is None or not built.get("votes"):
            print(f"[vinnytsia] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue

        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(built, f, ensure_ascii=False, indent=2)

        print(
            f"[vinnytsia] napisano {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
