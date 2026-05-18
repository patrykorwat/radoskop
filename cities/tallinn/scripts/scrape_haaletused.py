#!/usr/bin/env python3
"""
Scraper Tallinna Linnavolikogu hääletused via TEELE API.

Tallin publikuje pełne imienne głosowania przez publiczne REST API
teele.tallinn.ee/api. Brak auth, brak rate limitów, JSON natywnie
(z Accept: application/json). To najczystsze źródło z całego projektu,
porównywalne z data.gov.lt dla Wilna.

API endpoints:
    GET /api/meetings?unitId={UNIT}&pageSize=N&pageNumber=K&sortDirection=desc
        Lista sesji rady miejskiej. UNIT=2336 to Linnavolikogu 11. koosseis.
        Odpowiedź: {page, pageCount, pageSize, rowCount, results[]}.
        Każdy result ma: id, occurrenceDate, status.code, presentCount, absentCount.

    GET /api/meetings/{meetingId}/agendaitems
        Lista punktów porządku obrad sesji. Zwraca listę (nie paginowane).
        Każdy ma: id, position, name, completedVotesCount, decision.

    GET /api/meetings/{meetingId}/agendaitems/{itemId}/votes
        Lista głosowań w danym punkcie (zwykle 1, ale może być więcej).
        Każde: id, comment, inFavourCount, againstCount, neutralCount,
        absenteesCount, nonParticipantsCount, presentCount, startTime.

    GET /api/meetings/{meetingId}/agendaitems/{itemId}/votes/{voteId}/contents
        IMIENNE głosowanie. Lista 79 wpisów (komplet radnych).
        Każdy: result ENUM (INFAVOR|AGAINST|NEUTRAL|ABSENT|NOTPARTICIPATED),
        member.user.name (full name), faction.name (nazwa frakcji albo null).

Mapowanie result → kategoria Radoskop (z config.vote_text_map):
    INFAVOR         → za
    AGAINST         → przeciw
    NEUTRAL         → wstrzymal_sie
    ABSENT          → nieobecni
    NOTPARTICIPATED → brak_glosu

Mapowanie nazw frakcji do skrótów config.clubs:
    "Keskerakonna fraktsioon"                       → Kesk
    "Sotsiaaldemokraatliku Erakonna fraktsioon"     → SDE
    "Isamaa fraktsioon" / "Isamaa Erakonna ..."     → Isamaa
    "Reformierakonna fraktsioon" / "Eesti R..."     → Reform
    "Fraktsioon Parempoolsed" / "Parempoolsete..."  → Parempoolsed
    null / brak                                     → NZ (fraktsioonivaba)

Output: docs/kadencja-{id}.json w tym samym formacie co Wilno (sessions[],
votes[] z indexami do councilor_index[]). Generator strony radoskop konsumuje
dokładnie ten kształt.

Użycie:
    python3 scrape_haaletused.py
    python3 scrape_haaletused.py --kadencja-id 2025-2029
    python3 scrape_haaletused.py --max-sessions 3   # test mode
    python3 scrape_haaletused.py --skip-fetch       # użyj cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = "Mozilla/5.0 Radoskop/1.0 (+https://radoskop.eu)"
DEFAULT_TIMEOUT = 60
DEFAULT_PAGE_SIZE = 100
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.05

# Kategorie zgodne ze schemą Radoskop (te same co Wilno/Praga).
CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Mapowanie nazw frakcji TEELE → skróty config.clubs.
# Estonia: nazwy frakcji bywają nadawane różnie ("Fraktsioon X" vs
# "X-i Erakonna fraktsioon"), więc używamy prefix matching.
FACTION_NAME_TO_SLUG = (
    ("Keskerakonna", "Kesk"),
    ("Eesti Keskerakonna", "Kesk"),
    ("Sotsiaaldemokraatliku", "SDE"),
    ("Sotsiaaldemokraatlik", "SDE"),
    ("Isamaa", "Isamaa"),
    ("Reformierakonna", "Reform"),
    ("Eesti Reformierakonna", "Reform"),
    ("Parempoolsed", "Parempoolsed"),
    ("Parempoolsete", "Parempoolsed"),
    ("Fraktsioon Parempoolsed", "Parempoolsed"),
)


def faction_to_slug(faction_name: str | None) -> str:
    if not faction_name:
        return "NZ"
    name = faction_name.strip()
    for prefix, slug in FACTION_NAME_TO_SLUG:
        if prefix.lower() in name.lower():
            return slug
    return "NZ"


def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def http_get_json(
    url: str,
    cache_dir: Path | None,
    timeout: int = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> Any:
    """GET JSON z retry i opcjonalnym cache dyskowym."""
    cache_file: Path | None = None
    if cache_dir and use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.json"
        if cache_file.is_file():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cache_file.unlink()

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            break
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    else:
        raise RuntimeError(f"Failed after {RETRY_COUNT} attempts: {last_err}")

    data = json.loads(raw)
    if cache_file:
        cache_file.write_text(raw, encoding="utf-8")
    time.sleep(SLEEP_BETWEEN_CALLS)
    return data


# ---------------------------------------------------------------------------
# TEELE API wrappery
# ---------------------------------------------------------------------------

def list_meetings(
    api_base: str,
    unit_id: int,
    cache_dir: Path | None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Wszystkie sesje danego unit (kadencji). Paginacja przez pageNumber."""
    all_meetings: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"{api_base}/meetings?unitId={unit_id}"
            f"&pageSize={page_size}&pageNumber={page}&sortDirection=desc"
        )
        data = http_get_json(url, cache_dir, use_cache=False)
        results = data.get("results", []) if isinstance(data, dict) else []
        all_meetings.extend(results)
        page_count = data.get("pageCount", 1) if isinstance(data, dict) else 1
        if page >= page_count:
            break
        page += 1
    return all_meetings


def list_agenda_items(
    api_base: str,
    meeting_id: int,
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    url = f"{api_base}/meetings/{meeting_id}/agendaitems"
    data = http_get_json(url, cache_dir)
    if isinstance(data, list):
        return data
    return data.get("results", []) if isinstance(data, dict) else []


def list_votes_for_item(
    api_base: str,
    meeting_id: int,
    item_id: int,
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    url = f"{api_base}/meetings/{meeting_id}/agendaitems/{item_id}/votes"
    data = http_get_json(url, cache_dir)
    if isinstance(data, list):
        return data
    return data.get("results", []) if isinstance(data, dict) else []


def fetch_vote_contents(
    api_base: str,
    meeting_id: int,
    item_id: int,
    vote_id: int,
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    url = (
        f"{api_base}/meetings/{meeting_id}/agendaitems/{item_id}"
        f"/votes/{vote_id}/contents"
    )
    data = http_get_json(url, cache_dir)
    if isinstance(data, list):
        return data
    return data.get("results", []) if isinstance(data, dict) else []


# ---------------------------------------------------------------------------
# Buildery
# ---------------------------------------------------------------------------

def meeting_date(meeting: dict[str, Any]) -> str:
    occ = meeting.get("occurrenceDate") or ""
    return occ[:10]  # YYYY-MM-DD


def is_completed_session(meeting: dict[str, Any]) -> bool:
    """Filtr: tylko sesje COMPLETED, typ COUNCILSESSION."""
    if meeting.get("type") != "COUNCILSESSION":
        return False
    status = meeting.get("status") or {}
    return status.get("code") == "COMPLETED"


def kadencja_for_date(
    date_str: str,
    kadencje: dict[str, dict[str, Any]],
) -> str | None:
    if not date_str:
        return None
    sorted_kad = sorted(
        kadencje.items(),
        key=lambda kv: kv[1].get("start", ""),
        reverse=True,
    )
    for kid, kdef in sorted_kad:
        start = kdef.get("start", "")
        if start and date_str >= start:
            return kid
    return None


def build_kadencja(
    raw_votes: list[dict[str, Any]],
    meetings_by_id: dict[int, dict[str, Any]],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any]:
    """Złóż kadencję w schemacie Radoskop (sessions, votes, councilor_index).

    raw_votes: lista wewnętrznych wpisów {meeting_id, item, vote_meta, contents}.
    """
    vote_text_map = config.get("vote_text_map", {})
    kadencje = config.get("kadencje", {})

    # Pierwszy przelot: zbierz wszystkich radnych.
    all_names: set[str] = set()
    for rv in raw_votes:
        for row in rv["contents"]:
            user = (row.get("member") or {}).get("user") or {}
            name = (user.get("name") or "").strip()
            if name:
                all_names.add(name)

    councilor_index: list[str] = sorted(all_names)
    name_to_idx: dict[str, int] = {n: i for i, n in enumerate(councilor_index)}

    # Drugi przelot: agreguj na votes i sessions.
    votes_flat: list[dict[str, Any]] = []
    sessions_meta: dict[str, dict[str, Any]] = {}

    for rv in raw_votes:
        meeting = meetings_by_id.get(rv["meeting_id"]) or {}
        date = meeting_date(meeting)
        if not date or kadencja_for_date(date, kadencje) != kadencja_id:
            continue

        item = rv["item"]
        vote_meta = rv["vote_meta"]

        counts: dict[str, int] = {c: 0 for c in CATEGORIES}
        named_votes_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}

        for row in rv["contents"]:
            user = (row.get("member") or {}).get("user") or {}
            name = (user.get("name") or "").strip()
            if not name or name not in name_to_idx:
                continue
            result_enum = row.get("result") or ""
            cat = vote_text_map.get(result_enum)
            if not cat or cat not in counts:
                continue
            counts[cat] += 1
            named_votes_idx[cat].append(name_to_idx[name])

        decision = (item.get("decision") or {}).get("name") or ""

        vote_id = f"tallinn_{rv['meeting_id']}_{item.get('id')}_{vote_meta.get('id')}"
        source_url = (
            f"https://teele.tallinn.ee/meetings/council/sessions"
            f"/{rv['meeting_id']}/agenda?itemId={item.get('id')}"
        )

        votes_flat.append({
            "id": vote_id,
            "session_date": date,
            "session_number": date,  # numer = data; Tallin nie używa NR.
            "source_url": source_url,
            "topic": item.get("name") or vote_meta.get("comment") or "",
            "druk": str(item.get("id") or ""),
            "resolution": "",
            "result_native": decision,
            "counts": counts,
            "named_votes": named_votes_idx,
            "voted_at": vote_meta.get("startTime") or "",
        })

        # Session meta
        sess = sessions_meta.setdefault(date, {
            "date": date,
            "number": date,
            "title": f"Linnavolikogu istung {date}",
            "start": meeting.get("startTime"),
            "end": meeting.get("endTime"),
            "vote_ids": [],
            "attendees": set(),
        })
        sess["vote_ids"].append(vote_id)
        # Attendee = ktokolwiek głosował (poza ABSENT/NOTPARTICIPATED).
        for row in rv["contents"]:
            user = (row.get("member") or {}).get("user") or {}
            name = (user.get("name") or "").strip()
            result_enum = row.get("result") or ""
            if name and result_enum in {"INFAVOR", "AGAINST", "NEUTRAL"}:
                sess["attendees"].add(name)

    sessions: list[dict[str, Any]] = []
    for date, sess in sessions_meta.items():
        attendees_list = sorted(sess["attendees"])
        sessions.append({
            "date": date,
            "number": sess["number"],
            "title": sess["title"],
            "start": sess["start"],
            "end": sess["end"],
            "vote_count": len(sess["vote_ids"]),
            "attendee_count": len(attendees_list),
            "attendees": attendees_list,
            "source_url": f"https://teele.tallinn.ee/meetings/council/sessions",
        })
    sessions.sort(key=lambda s: s["date"])

    return {
        "sessions": sessions,
        "votes": votes_flat,
        "councilor_index": councilor_index,
    }


def build_profiles(
    raw_votes: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Profile radnych z aktualną frakcją (z ostatniego głosowania w którym wystąpili)."""
    # name → faction_name (najnowsze obserwowane)
    name_to_faction: dict[str, str] = {}
    name_to_first_seen: dict[str, str] = {}
    for rv in raw_votes:
        for row in rv["contents"]:
            user = (row.get("member") or {}).get("user") or {}
            name = (user.get("name") or "").strip()
            if not name:
                continue
            faction = (row.get("faction") or {}).get("name") or ""
            # Bierzemy faction z najnowszego głosowania (czyli ostatnie nadpisanie wygrywa).
            name_to_faction[name] = faction
            if name not in name_to_first_seen:
                name_to_first_seen[name] = ""

    profiles = {}
    for name, faction_name in name_to_faction.items():
        slug = faction_to_slug(faction_name)
        profiles[name] = {
            "name": name,
            "club": slug,
            "club_native": faction_name or "Fraktsioonivaba",
        }
    return profiles


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id", help="Konkretna kadencja, domyślnie wszystkie z config")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Limit sesji (tylko test mode)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Użyj cache zamiast pobierać świeże")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_base = config.get("teele_api_base") or "https://teele.tallinn.ee/api"
    unit_id = int(config.get("teele_unit_id") or 2336)
    cache = args.cache if not args.skip_fetch else None
    args.docs.mkdir(parents=True, exist_ok=True)

    print(f"[tallinn] list meetings unit={unit_id}", file=sys.stderr)
    meetings = list_meetings(api_base, unit_id, cache)
    completed = [m for m in meetings if is_completed_session(m)]
    print(f"[tallinn] {len(completed)}/{len(meetings)} sesji COMPLETED",
          file=sys.stderr)

    if args.max_sessions:
        completed = completed[: args.max_sessions]
        print(f"[tallinn] LIMIT: {len(completed)} sesji do scrape", file=sys.stderr)

    meetings_by_id = {m["id"]: m for m in completed}
    raw_votes: list[dict[str, Any]] = []

    for i, meeting in enumerate(completed, 1):
        meeting_id = meeting["id"]
        date = meeting_date(meeting)
        print(f"[tallinn] [{i}/{len(completed)}] sesja {date} (id={meeting_id})",
              file=sys.stderr)
        try:
            items = list_agenda_items(api_base, meeting_id, cache)
        except Exception as exc:
            print(f"  ERR agendaitems: {exc}", file=sys.stderr)
            continue

        voted_items = [it for it in items if it.get("completedVotesCount", 0) > 0]
        for item in voted_items:
            item_id = item["id"]
            try:
                votes = list_votes_for_item(api_base, meeting_id, item_id, cache)
            except Exception as exc:
                print(f"  ERR votes item {item_id}: {exc}", file=sys.stderr)
                continue
            for vote_meta in votes:
                if vote_meta.get("isCanceled"):
                    continue
                vote_id = vote_meta["id"]
                try:
                    contents = fetch_vote_contents(
                        api_base, meeting_id, item_id, vote_id, cache,
                    )
                except Exception as exc:
                    print(f"  ERR contents vote {vote_id}: {exc}", file=sys.stderr)
                    continue
                if not contents:
                    continue
                raw_votes.append({
                    "meeting_id": meeting_id,
                    "item": item,
                    "vote_meta": vote_meta,
                    "contents": contents,
                })

    print(f"[tallinn] zebrano {len(raw_votes)} głosowań imiennych",
          file=sys.stderr)

    # Posprzątaj stare pliki kadencji nieobjęte już configiem.
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            old.unlink()
            print(f"[tallinn] removed stale {old.name}", file=sys.stderr)

    kadencje_to_generate = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for kid in kadencje_to_generate:
        kdef = config["kadencje"][kid]
        built = build_kadencja(raw_votes, meetings_by_id, config, kid)
        if not built["votes"]:
            print(f"[tallinn] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue
        out = {
            "id": kid,
            "label": kdef.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": built["sessions"],
            "votes": built["votes"],
            "councilor_index": built["councilor_index"],
        }
        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[tallinn] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    # Zapisz profile (mapowanie name → faction).
    profiles = build_profiles(raw_votes, config)
    if profiles:
        profiles_path = args.docs / "profiles.json"
        with open(profiles_path, "w", encoding="utf-8") as f:
            json.dump({
                "scraped_at": scraped_at,
                "profiles": profiles,
            }, f, ensure_ascii=False, indent=2)
        print(f"[tallinn] wrote profiles.json: {len(profiles)} radnych",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
