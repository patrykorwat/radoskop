#!/usr/bin/env python3
"""
Scraper Klaipėdos miesto savivaldybės tarybos plenarki - wersja JSON API.

Klaipėda publikuje balsavimy w samorządowym portalu posiedzeń
posedziai.klaipeda.lt (vendor "epp" - elektroninis posėdžių portalas,
Angular SPA + Spring Boot backend). Backend bez auth, czyste JSON.

Backend base: https://posedziai.klaipeda.lt/api

Łańcuch endpointów (meeting -> question -> voting -> per-radny):

  GET /meetings/meetings?pageIndex={n}
      -> {"results":[{id, meetingNumber, meetingName, meetingTypeId,
                       happenedFrom (epoch ms), ...}]}
         meetingTypeId == -1 => TARYBOS POSĖDIS (plenarka). Inne typy
         (KONFERENCIJA itd.) pomijamy. Paginacja aż results puste.

  GET /questions/getAll?meetingId={id}
      -> [{questionId, questionNumber, documentNumber, questionTitle,
            resolution, considerationStart, ...}]   (porządek obrad)

  GET /voting/getVoting?questionId={qid}
      -> {votingId, votesFor, votesAgainst, votesRefrained, didNotVote,
          participantsNumber, voteResult, voteAccepted, ...}
         null jeśli dany punkt nie był głosowany imiennie.

  GET /votes/getVotes?votingId={vid}
      -> [{participantName, participantVote}]
         participantVote (z getVoteNumberByString w bundlu SPA):
            1 => Už        => za
            2 => Prieš     => przeciw
            3 => Susilaikė => wstrzymal_sie
            0 => Nebalsavo => brak_glosu
         Radny NIEOBECNY po prostu nie ma rekordu na liście (kategoria
         "nieobecni" liczona przez build_assembly_metrics z councilor_index).

Output: docs/kadencja-{id}.json w schemacie assembly-style (jak Wilno,
Praga, Tallinn). councilor_index = sorted unikalne nazwiska, named_votes
trzyma INDEKSY do tej listy. build_assembly_metrics.py liczy z tego
profile, frekwencję i zgodność z klubem.

Cache: per posiedzenie w .cache/meetings/{id}.json. Zamknięte posiedzenia
z głosami nie są pobierane ponownie. Świeże posiedzenie (młodsze niż
RADOSKOP_KLAIPEDA_REFRESH_DAYS, domyślnie 180 dni) bez głosów w cache jest
refetchowane, bo portal może publikować balsavimy z opóźnieniem.

Komitety (KOMITETO POSĖDIS) NIE są tu obsługiwane - jeśli kiedyś trzeba,
lecą przez osobny scraper w radoskop-premium/scrapers/komisje/ (patrz
feedback_komisje_location).

Użycie:
    python3 scrape_balsavimai.py
    python3 scrape_balsavimai.py --kadencja-id 2023-2027
    python3 scrape_balsavimai.py --skip-fetch        # użyj cache
    python3 scrape_balsavimai.py --max-pages 2        # tylko do testów
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
    _LT_TZ = ZoneInfo("Europe/Vilnius")
except Exception:  # pragma: no cover - fallback gdy brak tzdata
    _LT_TZ = None


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 45
# Grzeczność wobec portalu między requestami. Override przez
# RADOSKOP_KLAIPEDA_PAUSE (np. 0 do szybkich testów lokalnych).
REQUEST_PAUSE = float(os.environ.get("RADOSKOP_KLAIPEDA_PAUSE", "0.15"))
# Posiedzenie młodsze niż tyle dni, które w cache ma zero głosów, jest
# pobierane ponownie (głosy bywają publikowane z opóźnieniem). Starsze
# posiedzenia są zamknięte i payload z cache jest używany bezterminowo.
REFRESH_DAYS = float(os.environ.get("RADOSKOP_KLAIPEDA_REFRESH_DAYS", "180"))

# Mapowanie kodu głosu (participantVote) na wewnętrzny schemat Radoskop.
# Źródło: getVoteNumberByString w bundlu main.js portalu posedziai.klaipeda.lt.
VOTE_CODE_TO_CATEGORY = {
    1: "za",
    2: "przeciw",
    3: "wstrzymal_sie",
    0: "brak_glosu",
}

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Mapowanie wyniku (resolution / voteResult) na kategorię Radoskop.
RESULT_TEXT_TO_CATEGORY = {
    "Pritarė": "PRZYJETE",
    "Pritarta": "PRZYJETE",
    "Priėmė": "PRZYJETE",
    "Priimta": "PRZYJETE",
    "Nepritarė": "ODRZUCONE",
    "Nepritarta": "ODRZUCONE",
    "Nepriėmė": "ODRZUCONE",
    "Atmetė": "ODRZUCONE",
    "Atidėjo": "ODROCZONE",
    "Atidėta": "ODROCZONE",
    "Atidėtas": "ODROCZONE",
}


def normalize_result(text: str | None) -> str:
    """Mapuje resolution/voteResult na kategorię. Prefix-match dla wariantów."""
    if not text:
        return ""
    text = text.strip()
    if text in RESULT_TEXT_TO_CATEGORY:
        return RESULT_TEXT_TO_CATEGORY[text]
    if text.startswith(("Pritar", "Priėm", "Priim")):
        return "PRZYJETE"
    if text.startswith(("Nepritar", "Nepriėm", "Atmet")):
        return "ODRZUCONE"
    if text.startswith("Atidė"):
        return "ODROCZONE"
    return ""  # nieznany / "Informacija išklausyta" itd. - brak kategorii


def http_get_json(api_base: str, path: str, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Pobiera JSON z portalu. Zwraca sparsowaną strukturę lub None na 404."""
    url = f"{api_base}{path}"
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            if not raw.strip():
                return None
            return json.loads(raw)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            last_err = exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        wait = 2 ** attempt
        print(f"  retry {attempt + 1}/3 {path} after {wait}s ({last_err})",
              file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Failed after 3 attempts: {path}: {last_err}")


def epoch_ms_to_date(ms: int | None) -> str | None:
    """Epoch ms -> data YYYY-MM-DD w czasie lokalnym Klaipėdy (Europe/Vilnius)."""
    if not ms:
        return None
    if _LT_TZ is not None:
        dt = datetime.fromtimestamp(ms / 1000, tz=_LT_TZ)
    else:  # fallback: UTC+3 (posiedzenia zaczynają się rano/po południu lokalnie)
        dt = datetime.fromtimestamp(ms / 1000 + 3 * 3600, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def epoch_ms_to_iso(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def fetch_all_meetings(api_base: str, max_pages: int | None = None) -> list[dict]:
    """Pełna lista posiedzeń przez paginację pageIndex."""
    out: list[dict] = []
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break
        data = http_get_json(api_base, f"/meetings/meetings?pageIndex={page}")
        results = (data or {}).get("results") if isinstance(data, dict) else None
        if not results:
            break
        out.extend(results)
        print(f"  meetings page {page}: +{len(results)} (total {len(out)})",
              file=sys.stderr)
        page += 1
        time.sleep(REQUEST_PAUSE)
    return out


def fetch_meeting_payload(api_base: str, meeting: dict) -> dict:
    """Dla jednego posiedzenia: questions + voting + per-radny votes.

    Zwraca strukturę cache'owalną:
      {meeting: {...}, questions: [{question, voting, votes}]}
    """
    mid = meeting["id"]
    questions = http_get_json(api_base, f"/questions/getAll?meetingId={mid}") or []
    enriched: list[dict] = []
    for q in questions:
        qid = q.get("questionId")
        voting = http_get_json(api_base, f"/voting/getVoting?questionId={qid}")
        time.sleep(REQUEST_PAUSE)
        votes = None
        if voting and voting.get("votingId") is not None:
            votes = http_get_json(
                api_base, f"/votes/getVotes?votingId={voting['votingId']}"
            ) or []
            time.sleep(REQUEST_PAUSE)
        enriched.append({"question": q, "voting": voting, "votes": votes})
    return {"meeting": meeting, "questions": enriched}


def payload_vote_count(payload: dict) -> int:
    """Liczba punktów porządku z głosami imiennymi w payloadzie."""
    return sum(1 for item in payload.get("questions", []) if item.get("votes"))


def meeting_age_days(meeting: dict) -> float:
    """Wiek posiedzenia w dniach względem teraz. Brak daty traktuj jako stare."""
    ms = meeting.get("happenedFrom")
    if not ms:
        return float("inf")
    return (time.time() - ms / 1000) / 86400


def load_or_fetch(
    api_base: str,
    cache: Path,
    meeting_type_id: int,
    skip_fetch: bool,
    max_pages: int | None,
    earliest_start: str,
    max_meetings: int | None = None,
) -> list[dict]:
    """Zwraca listę payloadów posiedzeń (typu tarybos). Cache per posiedzenie
    w .cache/meetings/{id}.json.

    Zamknięte posiedzenie z głosami nie zmienia się, więc raz pobrany payload
    jest reużywany bezterminowo. Wyjątek: posiedzenie młodsze niż REFRESH_DAYS
    dni, którego payload w cache nie ma żadnych głosów, jest pobierane ponownie
    (wzorzec jak w scrape_notubiz.py dla Amsterdamu, gdzie głosy pojawiają się
    z opóźnieniem). Lista posiedzeń jest zawsze pobierana świeża, jest tania
    (jedna strona na ~150 posiedzeń) i wykrywa nowe plenarki.

    Posiedzenia przed `earliest_start` (najwcześniejszy start kadencji z config)
    są pomijane PRZED pobieraniem questions/votes - portal trzyma historię od
    2011, ale roll-call (per-radny) jest tylko dla bieżącej kadencji, a pliki
    kadencja-*.json i tak budują się wyłącznie z dat ze skonfigurowanych kadencji.
    """
    meetings_cache = cache / "meetings"
    legacy_file = cache / "meetings_raw.json"

    if skip_fetch:
        cached_files = sorted(meetings_cache.glob("*.json")) if meetings_cache.is_dir() else []
        if cached_files:
            payloads = []
            for fp in cached_files:
                with open(fp, "r", encoding="utf-8") as f:
                    payloads.append(json.load(f))
            payloads.sort(
                key=lambda p: p.get("meeting", {}).get("happenedFrom") or 0,
                reverse=True,
            )
            print(f"[klaipeda] using cache ({len(payloads)} posiedzeń)",
                  file=sys.stderr)
            return payloads
        if legacy_file.exists():
            print("[klaipeda] using legacy cache (meetings_raw.json)",
                  file=sys.stderr)
            with open(legacy_file, "r", encoding="utf-8") as f:
                return json.load(f)

    print("[klaipeda] fetch meetings list", file=sys.stderr)
    meetings = fetch_all_meetings(api_base, max_pages=max_pages)
    taryba = [m for m in meetings if m.get("meetingTypeId") == meeting_type_id]
    in_range = [
        m for m in taryba
        if (epoch_ms_to_date(m.get("happenedFrom")) or "") >= earliest_start
    ]
    in_range.sort(key=lambda m: m.get("happenedFrom") or 0, reverse=True)
    if max_meetings is not None:
        in_range = in_range[:max_meetings]
    print(
        f"[klaipeda] {len(meetings)} posiedzeń, {len(taryba)} TARYBOS POSĖDIS, "
        f"{len(in_range)} w zakresie kadencji (od {earliest_start})",
        file=sys.stderr,
    )

    meetings_cache.mkdir(parents=True, exist_ok=True)
    payloads: list[dict] = []
    reused = fetched = 0
    for i, m in enumerate(in_range, 1):
        cache_file = meetings_cache / f"{m['id']}.json"
        payload: dict | None = None
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[klaipeda] WARN: bad cache {cache_file.name}: {exc}",
                      file=sys.stderr)
                payload = None
        if payload is not None and payload_vote_count(payload) == 0 \
                and meeting_age_days(m) < REFRESH_DAYS:
            print(
                f"[klaipeda] meeting {i}/{len(in_range)}: "
                f"{m.get('meetingName')} (cache bez głosów, refetch)",
                file=sys.stderr,
            )
            payload = None
        if payload is None:
            print(f"[klaipeda] meeting {i}/{len(in_range)}: {m.get('meetingName')}",
                  file=sys.stderr)
            payload = fetch_meeting_payload(api_base, m)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            fetched += 1
        else:
            payload["meeting"] = m  # świeże meta z listy bez ruszania questions
            reused += 1
        payloads.append(payload)

    print(f"[klaipeda] cache: {reused} z cache, {fetched} pobranych",
          file=sys.stderr)
    if legacy_file.exists():
        try:
            legacy_file.unlink()
            print("[klaipeda] removed legacy meetings_raw.json", file=sys.stderr)
        except OSError:
            pass
    return payloads


def kadencja_for_date(date_str: str | None, kadencje: dict[str, dict]) -> str | None:
    if not date_str:
        return None
    for kid, kdef in sorted(
        kadencje.items(), key=lambda kv: kv[1].get("start", ""), reverse=True
    ):
        if date_str >= kdef.get("start", ""):
            return kid
    return None


def build_kadencja(
    payloads: list[dict], config: dict[str, Any], kadencja_id: str
) -> dict[str, Any]:
    """Buduje strukturę kadencji w schemacie assembly-style Radoskop."""
    kadencje = config.get("kadencje", {})
    aliases: dict[str, str] = config.get("name_aliases", {}) or {}

    def canon(name: str) -> str:
        n = (name or "").strip()
        return aliases.get(n, n)

    # Pierwszy przelot: zbierz wszystkich radnych tej kadencji + meta sesji.
    all_names: set[str] = set()
    sessions_meta: dict[str, dict[str, Any]] = {}

    for p in payloads:
        m = p["meeting"]
        date = epoch_ms_to_date(m.get("happenedFrom"))
        if not date or kadencja_for_date(date, kadencje) != kadencja_id:
            continue
        attendees: set[str] = set()
        vote_count = 0
        for item in p["questions"]:
            votes = item.get("votes")
            if not votes:
                continue
            vote_count += 1
            for v in votes:
                n = canon(v.get("participantName", ""))
                if n:
                    all_names.add(n)
                    attendees.add(n)
        # session_date jest kluczem sesji (jedna plenarka per dzień).
        if date not in sessions_meta:
            sessions_meta[date] = {
                "date": date,
                "number": str(m.get("meetingNumber") or ""),
                "title": m.get("meetingName") or "",
                "start": epoch_ms_to_iso(m.get("happenedFrom")),
                "end": epoch_ms_to_iso(m.get("happenedUntil")),
                "vote_count": 0,
                "attendees": set(),
                "source_url": "https://posedziai.klaipeda.lt/",
            }
        sessions_meta[date]["vote_count"] += vote_count
        sessions_meta[date]["attendees"] |= attendees

    councilor_index: list[str] = sorted(all_names)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    # Drugi przelot: flat votes z indeksami.
    votes_flat: list[dict[str, Any]] = []
    for p in payloads:
        m = p["meeting"]
        date = epoch_ms_to_date(m.get("happenedFrom"))
        if not date or kadencja_for_date(date, kadencje) != kadencja_id:
            continue
        for item in p["questions"]:
            votes = item.get("votes")
            if not votes:
                continue
            q = item["question"]
            voting = item.get("voting") or {}
            voting_id = voting.get("votingId")

            counts = {c: 0 for c in CATEGORIES}
            named = {c: [] for c in CATEGORIES}
            for v in votes:
                n = canon(v.get("participantName", ""))
                if not n or n not in name_to_idx:
                    continue
                cat = VOTE_CODE_TO_CATEGORY.get(v.get("participantVote"))
                if not cat:
                    continue
                counts[cat] += 1
                named[cat].append(name_to_idx[n])

            result_native = q.get("resolution") or voting.get("voteResult") or ""
            votes_flat.append({
                "id": f"klaipeda_{voting_id}",
                "session_date": date,
                "session_number": date,
                "source_url": "https://posedziai.klaipeda.lt/",
                "topic": (q.get("questionTitle") or "").strip(),
                "druk": str(q.get("documentNumber") or "") if q.get("documentNumber") else "",
                "resolution": "",
                "result": normalize_result(result_native),
                "result_native": result_native,
                "counts": counts,
                "named_votes": named,
                "voted_at": epoch_ms_to_iso(q.get("considerationStart")),
            })

    sessions: list[dict[str, Any]] = []
    for date, sess in sessions_meta.items():
        attendees_list = sorted(sess["attendees"])
        sessions.append({
            "date": sess["date"],
            "number": sess["number"],
            "title": sess["title"],
            "chair": "",
            "secretary": "",
            "start": sess["start"],
            "end": sess["end"],
            "vote_count": sess["vote_count"],
            "attendee_count": len(attendees_list),
            "attendees": attendees_list,
            "source_url": sess["source_url"],
        })
    sessions.sort(key=lambda s: (s["date"], s["title"]))

    return {
        "sessions": sessions,
        "votes": votes_flat,
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Użyj cache zamiast pobierać.")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Limit stron listy posiedzeń (tylko do testów).")
    parser.add_argument("--max-meetings", type=int, default=None,
                        help="Limit najnowszych posiedzeń do pobrania (tylko do testów).")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_base = config.get("posedziai_api_base", "https://posedziai.klaipeda.lt/api")
    meeting_type_id = config.get("posedziai_meeting_type_id", -1)

    args.cache.mkdir(parents=True, exist_ok=True)
    args.docs.mkdir(parents=True, exist_ok=True)

    kadencje = config.get("kadencje", {})
    earliest_start = min(
        (k.get("start", "9999") for k in kadencje.values()), default="0000-00-00"
    )

    payloads = load_or_fetch(
        api_base, args.cache, meeting_type_id, args.skip_fetch, args.max_pages,
        earliest_start, args.max_meetings,
    )

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    valid_ids = set(config.get("kadencje", {}).keys())
    for old_file in args.docs.glob("kadencja-*.json"):
        kid = old_file.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            try:
                old_file.unlink()
                print(f"[klaipeda] removed stale {old_file.name}", file=sys.stderr)
            except OSError as exc:
                print(f"[klaipeda] WARN: cannot remove {old_file.name}: {exc}",
                      file=sys.stderr)

    kadencje_to_gen = (
        [args.kadencja_id] if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    for kid in kadencje_to_gen:
        kadencja_def = config["kadencje"][kid]
        built = build_kadencja(payloads, config, kid)
        if not built["votes"]:
            print(f"[klaipeda] skip kadencja-{kid}: 0 balsavimų", file=sys.stderr)
            continue
        out = {
            "id": kid,
            "label": kadencja_def.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": built["sessions"],
            "votes": built["votes"],
            "councilor_index": built["councilor_index"],
        }
        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[klaipeda] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} balsavimų, "
            f"{len(built['councilor_index'])} narių",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
