#!/usr/bin/env python3
"""
Scraper głosowań Pražského zastupitelstva.

Praha publikuje dane przez czyste REST API zbudowane na Liferay 7
pod hostem `praha.eu`. Namespace `/o/prg/` zawiera endpointy do głosowań,
zastupitelů (radnych), klubów i sesji. Bez Playwright, czysty JSON.

Endpointy używane przez ten skrypt:
    GET /o/prg/representatives/period/{periodId}
        Pełna lista zastupitelů kadencji z fotkami i przynależnością klubową.

    GET /o/prg/voting/filter/meeting-numbers/period/{periodId}
        Lista numerów sesji wraz z formattedDate (DD.MM.YYYY).

    GET /o/prg/voting/search?periodId={pid}&pageNumber=N&pageSize=200
        Stronicowana lista głosowań w kadencji, każde z meetingNumber,
        printNumber, printName, votingDate, resolutionNumber, resultText.

    GET /o/prg/voting/detail/{votingId}/votes
        Imienna lista głosów: memberId, memberFullName, voteText.
        Wartości voteText: "Pro", "Proti", "Zdržel se", "Nehlasoval", "Nepřítomen".

Skrypt:
1. Czyta config.json (praga_period_id, vote_text_map, result_text_map).
2. Pobiera listę zastupitelů → buduje councilor_index oraz mapę memberId → name.
3. Pobiera listę meetingNumbers → mapa meetingNumber → date (ISO).
4. Pobiera wszystkie głosowania (paginowane).
5. Dla każdego głosowania pobiera /votes (z cache na dysku, plik per id).
6. Konwertuje na schemat Radoskop:
    - sessions[] grupowane po dacie (meetingNumber z meetings list),
      attendees union nie-Nepřítomen
    - votes[] z named_votes (indeksy w councilor_index per kategoria),
      counts (za/przeciw/wstrzymal_sie/brak_glosu/nieobecni)
    - councilor_index[] flat list nazwisk
7. Zapisuje docs/kadencja-{kadencja_id}.json.

UWAGA: praha.eu używa F5 ASM WAF. Po szybkiej serii zapytań blokuje IP
na kilkanaście minut. Cache na dysku (.cache/votes/{periodId}/{vid}.json)
sprawia że po blocku można po prostu uruchomić skrypt ponownie i pobierze
tylko te ID, których jeszcze nie ma w cache. Pierwszy pełny scrape
z 3974 głosowaniami przy SLEEP_BETWEEN=0.5s zajmie ~30 min.

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-votes 50
    python3 scrape_glosowania.py --period-id -33394 --kadencja-id 2018-2022
    python3 scrape_glosowania.py --output /tmp/k.json --cache-dir /tmp/praga_cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache" / "votes"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30
# Praha.eu używa F5 ASM WAF który po szybkiej serii zapytań (np. 30 w 30s)
# blokuje IP na kilkanaście minut zwracając "Request Rejected" HTML w 200 OK.
# Defaultowy sleep 0.5s daje ~7000s (2h) na cały scrape 3974 głosowań — wolno
# ale niezawodnie. Pierwsze pełne uruchomienie powinno iść z NAS, nie z laptopa.
SLEEP_BETWEEN = 0.5
PAGE_SIZE = 100


def http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT,
                  retries: int = 5) -> Any:
    """GET z retry i WAF detection.

    Praha.eu chroniona F5 ASM. Symptomy WAF block:
    1. HTTP 200 + Content-Type text/html + body "Request Rejected"
    2. HTTP 200 + pusty body
    Retryujemy z exponential backoff (do ~30s na ostatnim attempt).
    Po retries-out rzucamy RuntimeError.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "cs,en;q=0.5",
            })
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            if not body.strip():
                raise ValueError("empty body (WAF?)")
            if body.lstrip().startswith("<"):
                raise ValueError("HTML body (WAF block)")
            return json.loads(body)
        except (HTTPError, URLError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            wait = (2 ** attempt) * 1.0
            if attempt < retries - 1:
                print(f"      retry {attempt+1}/{retries} po {wait:.1f}s ({exc})", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"http_get_json fail after {retries} retries: {url} ({last_err})")


def normalize_name(full: str) -> str:
    """Praha API zwraca z tytułami przed nazwiskiem (z votes endpoint
    zazwyczaj jako "Tytuł Imię Nazwisko"). Strip everything that is
    a Czech academic title.

    Reguła: zachowujemy tokeny które wyglądają na imię/nazwisko.
    Token jest tytułem jeśli:
    - jest w liście znanych tytułów (z kropką lub bez)
    - kończy się na "." (typowy skrót akademicki)
    - składa się tylko z wielkich liter z kropkami (LL.M., Ph.D., M.A.)
    - składa się z par UPPER+lower jak Mgr, Bc, Ing, Dr, Ph
    """
    s = full.strip().rstrip(",")
    titles = {
        "Mgr", "Ing", "MUDr", "MVDr", "PhDr", "JUDr", "RNDr",
        "Bc", "BcA", "MgA", "doc", "prof", "PaedDr", "ThDr",
        "Dr", "Ph", "PhD", "CSc", "MBA", "MSc", "DiS", "DrSc",
        "arch", "et", "M", "A", "LL", "D", "h",
    }
    tokens = s.split()
    out = []
    for t in tokens:
        clean = t.rstrip(",").rstrip(".")
        # Kropki w środku (LL.M., Ph.D., M.A.) — strip them all.
        clean_no_dots = clean.replace(".", "")
        if clean_no_dots in titles:
            continue
        if clean.lower() in (x.lower() for x in titles):
            continue
        # Tokeny z kombinacją mała.duża jak "Ph.D" or "M.A": są też tytułami.
        if all(p in titles or p == "" for p in clean.split(".")):
            continue
        out.append(t.rstrip(","))
    return " ".join(out).strip(" ,")


def to_iso_date(czech_date: str) -> str | None:
    """'23.04.2026' → '2026-04-23'."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", czech_date.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s\-]", "", ascii_only.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "councilor"


def fetch_representatives(api_base: str, period_id: int) -> list[dict[str, Any]]:
    url = f"{api_base}/o/prg/representatives/period/{period_id}"
    return http_get_json(url)


def fetch_meetings(api_base: str, period_id: int) -> list[dict[str, str]]:
    url = f"{api_base}/o/prg/voting/filter/meeting-numbers/period/{period_id}"
    return http_get_json(url)


def fetch_votings_page(api_base: str, period_id: int, page_num: int,
                      page_size: int = PAGE_SIZE) -> dict[str, Any]:
    qs = urlencode({
        "periodId": period_id,
        "pageNumber": page_num,
        "pageSize": page_size,
    })
    url = f"{api_base}/o/prg/voting/search?{qs}"
    return http_get_json(url)


def fetch_all_votings(api_base: str, period_id: int,
                     max_votes: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    total = None
    while True:
        data = fetch_votings_page(api_base, period_id, page)
        if total is None:
            total = data.get("total", 0)
            print(f"  total głosowań: {total}", file=sys.stderr)
        items = data.get("votings", [])
        if not items:
            break
        out.extend(items)
        if max_votes and len(out) >= max_votes:
            out = out[:max_votes]
            break
        if len(out) >= total:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN)
    return out


def fetch_vote_detail(api_base: str, voting_id: int,
                     cache_dir: Path) -> list[dict[str, Any]]:
    cache_file = cache_dir / f"{voting_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    url = f"{api_base}/o/prg/voting/detail/{voting_id}/votes"
    data = http_get_json(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def build_kadencja(
    config: dict[str, Any],
    kadencja_id: str,
    period_id: int,
    cache_dir: Path,
    max_votes: int | None,
) -> dict[str, Any]:
    api_base = config.get("praga_api_base", "https://praha.eu")
    vote_map = config.get("vote_text_map", {})
    result_map = config.get("result_text_map", {})

    print(f"[1/4] Pobieram listę zastupitelů kadencji {kadencja_id} (period={period_id})", file=sys.stderr)
    reps = fetch_representatives(api_base, period_id)
    # Listy z /representatives/period zwracają "Tytuł Nazwisko Imię" (czeski
    # urzędniczy zwyczaj). Endpoint /voting/detail/{id}/votes z kolei zwraca
    # "Tytuł Imię Nazwisko" (forma naturalna). Wybieramy formę z votes jako
    # canonical, a representatives używamy do walidacji liczby zastupitelů
    # i pobierania klubu (osobny scrape_kluby.py). W głosowaniach mamy
    # memberId stabilne, więc lookup po id wystarczy.
    expected_member_ids: set[int] = set()
    for r in reps:
        rid = r.get("id")
        if rid is not None:
            expected_member_ids.add(rid)

    print(f"      → {len(expected_member_ids)} zastupitelů", file=sys.stderr)

    print(f"[2/4] Pobieram listę sesji", file=sys.stderr)
    meetings = fetch_meetings(api_base, period_id)
    # date_iso → lista meetingNumbers (na jednej dacie może być kilka,
    # np. mimořádne zasedání 5M, 4M na tej samej dacie 15.10.2025).
    date_to_meetings: dict[str, list[str]] = {}
    for m in meetings:
        num = m.get("meetingNumber", "")
        iso = to_iso_date(m.get("formattedDate", ""))
        if num and iso:
            date_to_meetings.setdefault(iso, []).append(num)
    print(f"      → {len(date_to_meetings)} dat sesji ({len(meetings)} meetingów)", file=sys.stderr)

    print(f"[3/4] Pobieram listę głosowań", file=sys.stderr)
    votings_meta = fetch_all_votings(api_base, period_id, max_votes=max_votes)
    print(f"      → {len(votings_meta)} głosowań", file=sys.stderr)

    print(f"[4/4] Pobieram imienne wyniki głosowań", file=sys.stderr)
    cache_period_dir = cache_dir / str(period_id)
    cache_period_dir.mkdir(parents=True, exist_ok=True)

    # Budujemy councilor_index dynamicznie ze strumienia głosowań.
    # Klucz to memberId (stabilne API id), wartość to indeks w
    # councilor_index. Nazwę bierzemy z pierwszego głosowania, w którym
    # ten member się pojawił (forma "Tytuł Imię Nazwisko" → po normalize
    # "Imię Nazwisko").
    member_id_to_index: dict[int, int] = {}
    councilor_index: list[str] = []

    sessions_acc: dict[str, dict[str, Any]] = {}
    votes_out: list[dict[str, Any]] = []

    progress_step = max(1, len(votings_meta) // 20)
    for i, vm in enumerate(votings_meta):
        if i % progress_step == 0 or i == len(votings_meta) - 1:
            print(f"      [{i+1}/{len(votings_meta)}]", file=sys.stderr)
        vid = vm.get("id")
        if vid is None:
            continue
        try:
            detail = fetch_vote_detail(api_base, vid, cache_period_dir)
        except (HTTPError, URLError, RuntimeError) as exc:
            print(f"      WARN: brak detail dla {vid}: {exc}", file=sys.stderr)
            continue
        time.sleep(SLEEP_BETWEEN)

        date_iso = to_iso_date(vm.get("votingDate", ""))
        # Search nie zwraca meetingNumber, używamy mapy z meetings list.
        # Większość dat ma jedną meetingNumber. Gdy więcej (M-meetings),
        # bierzemy pierwszą (regularne zasedání ma niższy numer niż M).
        candidate_meetings = sorted(date_to_meetings.get(date_iso or "", []),
                                    key=lambda m: (m.endswith("M"), m))
        meeting_num = candidate_meetings[0] if candidate_meetings else ""

        topic = vm.get("printName") or ""
        druk = vm.get("printNumber") or None
        resolution = vm.get("resolutionNumber") or None
        result_raw = vm.get("resultText") or ""
        result = result_map.get(result_raw, result_raw)

        named: dict[str, list[int]] = {
            "za": [], "przeciw": [], "wstrzymal_sie": [],
            "brak_glosu": [], "nieobecni": [],
        }

        for vote in detail:
            mid = vote.get("memberId")
            if mid is None:
                continue
            if mid not in member_id_to_index:
                # Pierwsze pojawienie. Z votes endpoint mamy
                # "Tytuł Imię Nazwisko"; po normalize → "Imię Nazwisko".
                mname = vote.get("memberFullName", "") or ""
                canonical = normalize_name(mname)
                member_id_to_index[mid] = len(councilor_index)
                councilor_index.append(canonical)
            cidx = member_id_to_index[mid]

            text = vote.get("voteText", "")
            cat = vote_map.get(text)
            if cat is None:
                cat = "brak_glosu"
            named[cat].append(cidx)

        counts = {k: len(v) for k, v in named.items()}

        # Sesja: bucketujemy po dacie. Numer sesji to meetingNumber
        # z tego dnia (regular > M). Gdy są dwa zasedánia tego samego
        # dnia, lump pod regularną; M-resolutions są wkrąglone.
        sess_key = date_iso or "unknown"
        if sess_key not in sessions_acc:
            sessions_acc[sess_key] = {
                "date": date_iso,
                "number": meeting_num,
                "all_numbers": candidate_meetings,
                "vote_count": 0,
                "attendees": set(),
                "source_url": (
                    f"{api_base}/vysledky-hlasovani#/?periodId={period_id}"
                    f"&meetingNumber={meeting_num}" if meeting_num
                    else f"{api_base}/vysledky-hlasovani"
                ),
            }
        sessions_acc[sess_key]["vote_count"] += 1
        # Attendees: każdy obecny (głosował lub Nehlasoval = był obecny
        # ale wstrzymał głos formalnie). Nepřítomen → out.
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for ci in named[cat]:
                sessions_acc[sess_key]["attendees"].add(councilor_index[ci])

        votes_out.append({
            "id": f"praga_{vid}",
            "session_date": date_iso,
            "session_number": meeting_num,
            "source_url": (
                f"{api_base}/vysledky-hlasovani#/?periodId={period_id}"
                f"&meetingNumber={meeting_num}&resolutionNumber={resolution or ''}"
                if meeting_num
                else f"{api_base}/vysledky-hlasovani"
            ),
            "topic": topic,
            "druk": druk,
            "resolution": resolution,
            "result": result,
            "counts": counts,
            "named_votes": named,
            "voted_at": date_iso,
        })

    # Finalizacja sessions
    sessions_out: list[dict[str, Any]] = []
    for key, s in sessions_acc.items():
        att = sorted(s["attendees"])
        sessions_out.append({
            "date": s["date"],
            "number": s["number"],
            "vote_count": s["vote_count"],
            "attendee_count": len(att),
            "attendees": att,
            "source_url": s["source_url"],
            "speakers": [],
            "dates_in_session": [s["date"]] if s["date"] else [],
        })
    # Sort sessions: po dacie rosnąco
    sessions_out.sort(key=lambda x: x["date"] or "")

    return {
        "id": kadencja_id,
        "label": config.get("kadencje", {}).get(kadencja_id, {}).get("label", kadencja_id),
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions": sessions_out,
        "total_sessions": len(sessions_out),
        "total_votes": len(votes_out),
        "total_councilors": len(councilor_index),
        "councilors": [],
        "votes": votes_out,
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--kadencja-id", default=None,
                        help="Default: kadencja_active z config.json")
    parser.add_argument("--period-id", type=int, default=None,
                        help="Praga API period id. Default: pobierane z config.kadencje[<id>].praga_period_id")
    parser.add_argument("--output", default=None,
                        help="Plik wyjściowy. Default: docs/kadencja-{id}.json")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--max-votes", type=int, default=None,
                        help="Limit do testowania")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"[scrape] brak config: {config_path}", file=sys.stderr)
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))

    kadencja_id = args.kadencja_id or config.get("kadencja_active")
    if not kadencja_id:
        print("[scrape] brak kadencja_active w config.json i nie podano --kadencja-id", file=sys.stderr)
        return 1

    kadencja_meta = config.get("kadencje", {}).get(kadencja_id, {})
    period_id = args.period_id or kadencja_meta.get("praga_period_id") or config.get("praga_period_id")
    if not period_id:
        print(f"[scrape] brak period_id dla kadencji {kadencja_id}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else DEFAULT_DOCS / f"kadencja-{kadencja_id}.json"
    cache_dir = Path(args.cache_dir)

    out = build_kadencja(
        config=config,
        kadencja_id=kadencja_id,
        period_id=int(period_id),
        cache_dir=cache_dir,
        max_votes=args.max_votes,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] zapisano {output_path} (sesji={out['total_sessions']}, glosowan={out['total_votes']}, radnych={out['total_councilors']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
