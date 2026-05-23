#!/usr/bin/env python3
"""
Scraper Gemeenteraad Amsterdam via amsterdam.raadsinformatie.nl (NotuBiz).

Amsterdam publikuje pełne imienne głosowania w HTML stron vergadering.
Brak dedykowanego JSON API per-radny — dane są server-side rendered.

Źródło listy sesji:
    ORI ElasticSearch (openraadsinformatie.nl) — indeks ori_amsterdam_*
    Filtr: @type=Meeting, name=RAAD, start_date >= kadencja start.
    Pole was_generated_by.original_identifier = notubiz meeting ID.

Źródło głosowań:
    https://amsterdam.raadsinformatie.nl/vergadering/{meeting_id}
    Każda strona zawiera bloki <div id="voting_N"> z sekcjami votes_parties:
        <div class="votes_list voor">  → za
        <div class="votes_list against"> → przeciw
        <div class="votes_list onthouden"> → wstrzymal_sie
    Wewnątrz każdej sekcji: per-partia listy radnych z klasą CSS odpowiadającą wyniku.
    Nieobecni = councilor_count - suma głosów (wyciągane z aria-label).

Mapowanie party_name → slug w config.party_name_to_slug.

Output: docs/kadencja-{id}.json w standardowym schemacie Radoskop.

Użycie:
    python3 scrape_notubiz.py
    python3 scrape_notubiz.py --kadencja-id 2022-2026
    python3 scrape_notubiz.py --max-sessions 3    # tryb testowy
    python3 scrape_notubiz.py --skip-fetch         # tylko cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
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
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.3

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# CSS class na HTML -> kategoria Radoskop.
# Amsterdam używa angielskich nazw klas CSS (voor/against/abstain),
# mimo że etykiety w UI są po niderlandzku.
CSS_CLASS_TO_VOTE = {
    "voor": "za",
    "against": "przeciw",
    "abstain": "wstrzymal_sie",
}

# Wynik głosowania (z aria-label) -> stała Radoskop
RESULT_KEYWORDS = {
    "aangenomen": "PRZYJETE",
    "verworpen": "ODRZUCONE",
    "aangehouden": "ODROCZONE",
    "ingetrokken": "ODROCZONE",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def http_get(
    url: str,
    cache_dir: Path | None,
    timeout: int = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    accept: str = "text/html,application/json",
) -> str:
    """GET z retry i opcjonalnym cache."""
    cache_file: Path | None = None
    if cache_dir and use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.html"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "nl,en;q=0.8",
    })
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    else:
        raise RuntimeError(f"Failed after {RETRY_COUNT} attempts: {last_err}")

    if cache_file:
        cache_file.write_text(raw, encoding="utf-8")
    time.sleep(SLEEP_BETWEEN_CALLS)
    return raw


def http_get_json(url: str, cache_dir: Path | None, **kwargs: Any) -> Any:
    raw = http_get(url, cache_dir, accept="application/json", **kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# ORI ElasticSearch — lista sesji RAAD
# ---------------------------------------------------------------------------

def _ori_index(config: dict[str, Any]) -> str:
    """Zwróć nazwę indeksu ES z configa lub wykryj dynamicznie."""
    stored = config.get("ori_es_index", "")
    if stored:
        return stored
    # Fallback: wykryj najnowszy indeks amsterdam
    base = config.get("ori_es_base", "https://api.openraadsinformatie.nl/v1/elastic")
    indices_url = f"{base}/_cat/indices?format=json"
    try:
        data = http_get_json(indices_url, None, use_cache=False)
        ams = [d["index"] for d in data if d.get("index", "").startswith("ori_amsterdam_")
               and "west" not in d["index"] and "oost" not in d["index"]
               and "noord" not in d["index"] and "zuid" not in d["index"]
               and "nieuw" not in d["index"]]
        if ams:
            return sorted(ams)[-1]
    except Exception as exc:
        print(f"  [warn] ORI index discovery failed: {exc}", file=sys.stderr)
    return "ori_amsterdam_*"


def fetch_raad_meetings(
    config: dict[str, Any],
    kadencja_start: str,
    cache_dir: Path | None,
) -> list[dict[str, str]]:
    """Pobierz listę sesji RAAD z ORI ES od daty kadencji."""
    base = config.get("ori_es_base", "https://api.openraadsinformatie.nl/v1/elastic")
    index = _ori_index(config)
    url = f"{base}/{index}/_search"

    query = {
        "size": 500,
        "query": {
            "bool": {
                "must": [
                    {"match": {"@type": "Meeting"}},
                    {"match": {"name": "RAAD"}}
                ],
                "filter": {
                    "range": {
                        "start_date": {
                            "gte": f"{kadencja_start}T00:00:00+01:00"
                        }
                    }
                }
            }
        },
        "_source": ["name", "start_date", "was_generated_by"],
        "sort": [{"start_date": {"order": "asc"}}]
    }

    import urllib.request as _ur
    req = _ur.Request(
        url,
        data=json.dumps(query).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    # Nie cachujemy listy sesji — zawsze świeże
    with _ur.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    meetings = []
    for h in data.get("hits", {}).get("hits", []):
        s = h.get("_source", {})
        wgb = s.get("was_generated_by") or {}
        orig_id = wgb.get("original_identifier") if isinstance(wgb, dict) else None
        date_str = str(s.get("start_date") or "")[:10]
        if orig_id and date_str >= kadencja_start:
            meetings.append({"date": date_str, "id": str(orig_id), "name": s.get("name", "")})

    meetings.sort(key=lambda m: m["date"])
    return meetings


# ---------------------------------------------------------------------------
# Parser HTML vergadering
# ---------------------------------------------------------------------------

def _parse_result(aria_label: str, result_map: dict[str, str]) -> str:
    """Wyciągnij wynik głosowania z aria-label."""
    text = aria_label.lower()
    for kw, result in result_map.items():
        if kw.lower() in text:
            return result
    return ""


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def parse_voting_blocks(
    html: str,
    meeting_id: str,
    meeting_date: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Parsuje wszystkie bloki <div id="voting_N"> ze strony vergadering.
    Zwraca listę wpisów głosowań w formacie wewnętrznym.
    """
    party_map: dict[str, str] = config.get("party_name_to_slug", {})
    result_map: dict[str, str] = config.get("result_text_map", {})
    vote_map: dict[str, str] = config.get("vote_text_map", {})
    councilor_count: int = config.get("councilor_count", 45)

    # Każdy blok głosowania to: <div id="voting_N" ...>...</div>
    # Kończymy gdy zaczyna się kolejny voting_ lub koniec listy
    block_pattern = re.compile(
        r'<div\s+id="voting_(\d+)"[^>]*>(.*?)(?=<div\s+id="voting_\d+"|$)',
        re.S,
    )

    result_votes = []

    for m in block_pattern.finditer(html):
        voting_id = m.group(1)
        block = m.group(2)

        # Tytuł: <h5>...<span class="icon-edit">Stemming</span> [TEMAT]...</h5>
        title_m = re.search(
            r'<h5>[^<]*<span[^>]*>[^<]*</span>\s*(.*?)</h5>', block, re.S
        )
        topic = _strip_tags(title_m.group(1)).strip() if title_m else ""

        # Wynik z aria-label na div role="figure"
        aria_m = re.search(r'<div\s+role="figure"\s+aria-label="([^"]+)"', block)
        aria_text = aria_m.group(1) if aria_m else ""
        result = _parse_result(aria_text, result_map)

        # Nieobecni z aria-label: "afwezig: N stemmen"
        afwezig_m = re.search(r'afwezig:\s*(\d+)', aria_text, re.I)
        afwezig = int(afwezig_m.group(1)) if afwezig_m else 0

        # Sekcje votes_list: voor / against / onthouden
        named_by_cat: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        counts: dict[str, int] = {c: 0 for c in CATEGORIES}

        # votes_list zawiera tylko ul/li/h5 — zero nested div,
        # więc pierwszy </div> zamyka tę sekcję.
        section_pattern = re.compile(
            r'<div\s+class="votes_list\s+(\w+)">(.*?)</div>',
            re.S,
        )
        for sec_m in section_pattern.finditer(block):
            css_class = sec_m.group(1)    # "voor", "against", "onthouden"
            sec_html = sec_m.group(2)

            # Mapowanie CSS class → kategoria Radoskop
            cat = CSS_CLASS_TO_VOTE.get(css_class)
            if not cat:
                continue

            # Per-partia: <li tabindex="0">PARTY<ul>...<li class="CSS">Naam</li>...</ul></li>
            party_block_pattern = re.compile(
                r'<li\s+tabindex="0">([^<]+?)\s*<ul>(.*?)</ul>\s*</li>',
                re.S,
            )
            for pb in party_block_pattern.finditer(sec_html):
                # party_name nie jest potrzebna per-radny (wystarczy klasa)
                members_html = pb.group(2)
                member_names = re.findall(
                    r'<li\s+class="(?:voor|against|abstain)">([^<]+)</li>',
                    members_html,
                )
                for name in member_names:
                    name = name.strip()
                    if name:
                        named_by_cat[cat].append(name)

        # Zlicz
        for cat, names in named_by_cat.items():
            counts[cat] = len(names)

        counts["nieobecni"] = afwezig

        # Brak głosu = reszta (jeśli suma nie zgadza się z councilor_count)
        voted = sum(counts[c] for c in ("za", "przeciw", "wstrzymal_sie", "nieobecni"))
        counts["brak_glosu"] = max(0, councilor_count - voted)

        # Buduj zbiór wszystkich radnych z tego głosowania
        all_names: set[str] = set()
        for names in named_by_cat.values():
            all_names.update(names)

        result_votes.append({
            "voting_id": voting_id,
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "topic": topic,
            "result": result,
            "counts": counts,
            "named_by_cat": named_by_cat,
            "all_names": all_names,
        })

    return result_votes


def fetch_vergadering(
    meeting: dict[str, str],
    config: dict[str, Any],
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    """Pobierz i sparsuj stronę vergadering. Zwraca listę głosowań."""
    base = config.get("notubiz_raadsinformatie_base", "https://amsterdam.raadsinformatie.nl")
    url = f"{base}/vergadering/{meeting['id']}"
    html = http_get(url, cache_dir)
    blocks = parse_voting_blocks(html, meeting["id"], meeting["date"], config)
    return blocks


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_kadencja(
    all_votes: list[dict[str, Any]],
    meetings: list[dict[str, str]],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any]:
    """Złóż kadencję w schemacie Radoskop."""
    kadencja_start = config["kadencje"][kadencja_id]["start"]

    # Filtruj do tej kadencji
    votes_in_kad = [
        v for v in all_votes
        if v["meeting_date"] >= kadencja_start
    ]

    # Zbierz wszystkich radnych
    all_names: set[str] = set()
    for v in votes_in_kad:
        all_names.update(v.get("all_names", set()))
    councilor_index = sorted(all_names)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    # Zbuduj votes_flat
    votes_flat: list[dict[str, Any]] = []
    sessions_meta: dict[str, dict[str, Any]] = {}
    base = config.get("notubiz_raadsinformatie_base", "https://amsterdam.raadsinformatie.nl")

    for v in votes_in_kad:
        date = v["meeting_date"]
        mid = v["meeting_id"]
        vid = v["voting_id"]

        # named_votes jako słownik {kat: [indeksy]}
        named_votes_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}
        for cat, names in v["named_by_cat"].items():
            for name in names:
                if name in name_to_idx:
                    named_votes_idx[cat].append(name_to_idx[name])

        vote_id = f"amsterdam_{mid}_{vid}"
        source_url = f"{base}/vergadering/{mid}"

        votes_flat.append({
            "id": vote_id,
            "session_date": date,
            "session_number": date,
            "source_url": source_url,
            "topic": v["topic"],
            "druk": vid,
            "resolution": "",
            "result_native": v["result"],
            "counts": v["counts"],
            "named_votes": named_votes_idx,
        })

        # Session meta
        sess = sessions_meta.setdefault(date, {
            "date": date,
            "number": date,
            "title": f"Gemeenteraad Amsterdam {date}",
            "vote_ids": [],
            "attendees": set(),
            "source_url": source_url,
        })
        sess["vote_ids"].append(vote_id)
        for name in v.get("all_names", set()):
            sess["attendees"].add(name)

    sessions: list[dict[str, Any]] = []
    for date, sess in sorted(sessions_meta.items()):
        att_list = sorted(sess["attendees"])
        sessions.append({
            "date": date,
            "number": sess["number"],
            "title": sess["title"],
            "start": None,
            "end": None,
            "vote_count": len(sess["vote_ids"]),
            "attendee_count": len(att_list),
            "attendees": att_list,
            "source_url": sess["source_url"],
        })

    return {
        "sessions": sessions,
        "votes": votes_flat,
        "councilor_index": councilor_index,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id", help="Konkretna kadencja (domyślnie aktywna)")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Limit sesji do scrape (tryb testowy)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Użyj tylko cache, nie pobieraj świeżych danych")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    args.docs.mkdir(parents=True, exist_ok=True)
    cache = args.cache if not args.skip_fetch else None

    kadencja_id = args.kadencja_id or config.get("kadencja_active", "2022-2026")
    kadencja_start = config["kadencje"][kadencja_id]["start"]

    print(f"[amsterdam] pobieranie listy sesji RAAD od {kadencja_start} z ORI",
          file=sys.stderr)
    meetings = fetch_raad_meetings(config, kadencja_start, cache)
    print(f"[amsterdam] znaleziono {len(meetings)} sesji RAAD", file=sys.stderr)

    if args.max_sessions:
        meetings = meetings[: args.max_sessions]
        print(f"[amsterdam] LIMIT: {len(meetings)} sesji do scrape", file=sys.stderr)

    all_votes: list[dict[str, Any]] = []

    for i, meeting in enumerate(meetings, 1):
        print(
            f"[amsterdam] [{i}/{len(meetings)}] sesja {meeting['date']} "
            f"(id={meeting['id']})",
            file=sys.stderr,
        )
        try:
            votes = fetch_vergadering(meeting, config, cache)
        except Exception as exc:
            print(f"  ERR vergadering {meeting['id']}: {exc}", file=sys.stderr)
            continue

        voted = [v for v in votes if any(v["counts"][c] > 0 for c in ("za", "przeciw", "wstrzymal_sie"))]
        print(f"  {len(voted)} głosowań imiennych", file=sys.stderr)
        all_votes.extend(voted)

    print(f"[amsterdam] łącznie {len(all_votes)} głosowań", file=sys.stderr)

    # Usuń stare pliki kadencji nieobjęte configiem
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            old.unlink()
            print(f"[amsterdam] usunięto stały {old.name}", file=sys.stderr)

    kadencje_to_generate = (
        [kadencja_id] if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for kid in kadencje_to_generate:
        kdef = config["kadencje"][kid]
        built = build_kadencja(all_votes, meetings, config, kid)
        if not built["votes"]:
            print(f"[amsterdam] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
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
            f"[amsterdam] zapisano {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
