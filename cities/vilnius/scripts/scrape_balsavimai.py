#!/usr/bin/env python3
"""
Scraper Vilniaus miesto savivaldybės tarybos plenarki - wersja open-data API.

UWAGA: Ten skrypt obsługuje TYLKO plenarkę (Vilniaus miesto savivaldybės
tarybos posėdis). Komitety lecą przez osobny scraper w
`radoskop-premium/scrapers/komisje/vilnius.py` (backend datagovlt_lt).
Patrz feedback_komisje_location: komisje ZAWSZE w premium pakiecie.

Vilnius publikuje balsavimy w państwowym portalu otwartych danych
data.gov.lt (Valstybės duomenų agentūra). Dataset ID 3849, dwie tabele:

- Klausimas: posiedzenie + sprawa (pytanie do głosowania) + sumaryczne wyniki
- Balsas:    indywidualny głos radnego dla danego balsavimo_id

Tabele linkowane przez balsavimo_id.

Endpoint:
    https://get.data.gov.lt/datasets/gov/vilniaus_m_sav/balsavimai/{Klausimas|Balsas}/:format/csv

Schema Klausimas:
    vda_id, posedis, posedzio_statusas, pirmininkas, sekretorius,
    prasidejo, baigesi, klausimo_id, klausimas_lt, svarstymas,
    svarstymo_tvarka, balsavimo_id, balsavo, nebalsavo, dalyvavo,
    reikia_daugumai, sprendimas

Schema Balsas:
    vda_id, balsavimo_id, balsavimo_laikas, narys, balsas

Mapowanie do schematu Radoskop (vote_text_map):
    Už          → za
    Prieš       → przeciw
    Susilaikė   → wstrzymal_sie
    Nebalsavo   → brak_glosu
    (nieobecny radny po prostu nie ma rekordu w Balsas)

Mapowanie wyniku (result_text_map):
    Pritarė     → PRZYJETE
    Nepritarė   → ODRZUCONE
    Atidėjo     → ODROCZONE

Klasyfikacja sesji: regex TARYBA_PATTERN łapie wyłącznie
"VILNIAUS MIESTO SAVIVALDYBĖS TARYBOS POSĖDIS NR. X". Wszystko inne
(zawiera "KOMITETO POSĖDIS") jest IGNOROWANE - leci przez premium scraper.

Użycie:
    python3 scrape_balsavimai.py
    python3 scrape_balsavimai.py --kadencja-id 2023-2027
    python3 scrape_balsavimai.py --limit 1000  # tylko do testów
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 60
PAGE_SIZE = 500  # rekordy per request; 1000 też działa ale 500 to bezpieczny default

API_BASE = "https://get.data.gov.lt/datasets/gov/vilniaus_m_sav/balsavimai"

# Mapowanie wartości głosu z LT do wewnętrznego schematu Radoskop.
VOTE_TEXT_TO_CATEGORY = {
    "Už": "za",
    "Prieš": "przeciw",
    "Susilaikė": "wstrzymal_sie",
    "Nebalsavo": "brak_glosu",
}

# Mapowanie wyniku decyzji. Wilno używa wielu wariantów, normalizujemy.
RESULT_TEXT_TO_CATEGORY = {
    "Pritarė": "PRZYJETE",
    "Pritarė su pataisa": "PRZYJETE",
    "Pritarė su pataisomis": "PRZYJETE",
    "Pritarė su siūlymais": "PRZYJETE",
    "Priėmė": "PRZYJETE",
    "Priėmė su protokoliniu nutarimu": "PRZYJETE",
    "Priėmė sprendimą su protokoliniu įrašu": "PRZYJETE",
    "Priimta bendru sutarimu": "PRZYJETE",
    "Išduoti leidimą": "PRZYJETE",
    "Nepritarė": "ODRZUCONE",
    "Nepriėmė": "ODRZUCONE",
    "Neišduoti leidimo": "ODRZUCONE",
    "Atidėjo": "ODROCZONE",
    "Atidėtas": "ODROCZONE",
}


def normalize_sprendimas(text: str) -> str:
    """Mapuje sprendimas na kategorię. Fallback: prefix matching dla wariantów."""
    if not text:
        return ""
    if text in RESULT_TEXT_TO_CATEGORY:
        return RESULT_TEXT_TO_CATEGORY[text]
    # Prefix matching dla nieznanych wariantów typu "Pritarė su [czymś nowym]"
    if text.startswith("Pritarė") or text.startswith("Priėmė") or text.startswith("Priimta"):
        return "PRZYJETE"
    if text.startswith("Nepritarė") or text.startswith("Nepriėmė") or text.startswith("Atmetė"):
        return "ODRZUCONE"
    if text.startswith("Atidėj") or text.startswith("Atidėt"):
        return "ODROCZONE"
    return text  # nieznany - zostaw raw

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Pattern do klasyfikacji posiedzeń.
# "VILNIAUS MIESTO SAVIVALDYBĖS TARYBOS POSĖDIS NR. 31" → Taryba
# "VILNIAUS MIESTO SAVIVALDYBĖS MIESTO PLĖTROS KOMITETO POSĖDIS NR. 31" → komitet
TARYBA_PATTERN = re.compile(
    r"VILNIAUS\s+MIESTO\s+SAVIVALDYB[ĖE]S\s+TARYBOS\s+POS[ĖE]DIS",
    re.IGNORECASE,
)


def http_get_csv(url: str, timeout: int = DEFAULT_TIMEOUT) -> list[dict[str, str]]:
    """Pobiera CSV i parsuje. Zwraca wszystkie wiersze."""
    print(f"  GET {url}", file=sys.stderr)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,*/*",
    })
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8")
            break
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/3 after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    else:
        raise RuntimeError(f"Failed after 3 attempts: {last_err}")

    reader = csv.DictReader(io.StringIO(data))
    return list(reader)


def fetch_table(table: str, limit: int | None = None) -> list[dict[str, str]]:
    """Pobiera tabelę z data.gov.lt.

    Spinta (silnik get.data.gov.lt) ma kapryśną składnię URQL — pagination
    przez `page('CURSOR')` i `limit(N)` nie chce się chainować bo `&` w
    URQL jest operatorem AND, nie separatorem URL. W praktyce najlepiej
    działa pojedynczy strzał bez kursora.

    Dla `--limit N` używamy `limit(N)`. Bez limita robimy jeden duży
    request (timeout do 5 minut).

    Jeśli kiedyś trzeba paginować dla bardzo dużych tabel, opcją jest
    filter po `prasidejo>'YYYY-MM-DD'` chunked per rok.
    """
    if limit is not None:
        query = f"limit({limit})"
        url = f"{API_BASE}/{table}/%3Aformat/csv?{quote(query, safe='=,')}"
        return http_get_csv(url, timeout=DEFAULT_TIMEOUT)

    # Pełen dump w jednym requeście. Wyższy timeout.
    url = f"{API_BASE}/{table}/%3Aformat/csv"
    return http_get_csv(url, timeout=300)


def parse_dt(value: str | None) -> str | None:
    """Normalizuje timestamp do ISO 8601 (zachowuje to co dostaliśmy)."""
    if not value:
        return None
    return value


def session_date(prasidejo: str | None) -> str | None:
    """Wyciąga datę (YYYY-MM-DD) z timestampa."""
    if not prasidejo:
        return None
    return prasidejo[:10]


def is_taryba(posedis: str) -> bool:
    """True jeśli to posiedzenie Tarybos (rada miejska), False jeśli komitet."""
    if not posedis:
        return False
    return bool(TARYBA_PATTERN.search(posedis))


def kadencja_for_date(date_str: str | None, kadencje: dict[str, dict]) -> str | None:
    """Dopasowuje datę sesji do kadencji."""
    if not date_str:
        return None
    # Sortuj kadencje po start descending, pierwsza pasująca wygrywa.
    sorted_kad = sorted(
        kadencje.items(),
        key=lambda kv: kv[1].get("start", ""),
        reverse=True,
    )
    for kid, kdef in sorted_kad:
        start = kdef.get("start", "")
        if date_str >= start:
            return kid
    return None


def build_glosowanie(
    klausimas_row: dict[str, str],
    balsas_rows: list[dict[str, str]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Buduje pojedyncze głosowanie w schemacie Radoskop."""
    glos: dict[str, Any] = {
        "id": klausimas_row.get("balsavimo_id") or klausimas_row.get("klausimo_id"),
        "klausimo_id": klausimas_row.get("klausimo_id"),
        "balsavimo_id": klausimas_row.get("balsavimo_id"),
        "tytul": klausimas_row.get("klausimas_lt", ""),
        "wynik_native": klausimas_row.get("sprendimas", ""),
        "wynik": normalize_sprendimas(klausimas_row.get("sprendimas", "")),
        "reikia_daugumai": klausimas_row.get("reikia_daugumai", ""),
    }

    counts: dict[str, int] = {c: 0 for c in CATEGORIES}
    named_votes: dict[str, list[str]] = {c: [] for c in CATEGORIES}

    for b in balsas_rows:
        narys = (b.get("narys") or "").strip()
        if not narys:
            continue
        cat = VOTE_TEXT_TO_CATEGORY.get(b.get("balsas", ""))
        if not cat:
            continue
        counts[cat] += 1
        named_votes[cat].append(narys)

    glos["counts"] = counts
    glos["named_votes"] = named_votes
    return glos


def build_sessions(
    klausimas_rows: list[dict[str, str]],
    balsas_by_vote: dict[str, list[dict[str, str]]],
    config: dict[str, Any],
    kadencja_id: str,
) -> list[dict[str, Any]]:
    """Grupuje pytania w sesje po (data, posedis)."""
    kadencje = config.get("kadencje", {})
    sessions_map: dict[tuple[str, str], dict[str, Any]] = {}

    for k in klausimas_rows:
        posedis = k.get("posedis", "")
        date = session_date(k.get("prasidejo"))
        if not date or not posedis:
            continue
        # Filtruj tylko sesje pasujące do tej kadencji
        if kadencja_for_date(date, kadencje) != kadencja_id:
            continue

        key = (date, posedis)
        if key not in sessions_map:
            sessions_map[key] = {
                "date": date,
                "title": posedis,
                "status": k.get("posedzio_statusas", ""),
                "chair": k.get("pirmininkas", ""),
                "secretary": k.get("sekretorius", ""),
                "start": k.get("prasidejo"),
                "end": k.get("baigesi"),
                "votes": [],
                "attendees": set(),
            }
        balsavimo_id = k.get("balsavimo_id")
        if not balsavimo_id:
            # Pytanie bez głosowania (np. omówienie). Nadal liczy się jako punkt.
            continue
        balsas_rows = balsas_by_vote.get(balsavimo_id, [])
        glos = build_glosowanie(k, balsas_rows, config)
        sessions_map[key]["votes"].append(glos)
        for b in balsas_rows:
            n = (b.get("narys") or "").strip()
            if n:
                sessions_map[key]["attendees"].add(n)

    # Konwersja set → sorted list, dodanie count fields.
    sessions: list[dict[str, Any]] = []
    for sess in sessions_map.values():
        attendees_list = sorted(sess["attendees"])
        sessions.append({
            "date": sess["date"],
            "title": sess["title"],
            "status": sess["status"],
            "chair": sess["chair"],
            "secretary": sess["secretary"],
            "start": sess["start"],
            "end": sess["end"],
            "vote_count": len(sess["votes"]),
            "attendee_count": len(attendees_list),
            "attendees": attendees_list,
            "votes": sess["votes"],
        })

    sessions.sort(key=lambda s: (s["date"], s["title"]))
    return sessions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--kadencja-id",
        help="Konkretna kadencja do wygenerowania. Domyślnie wszystkie z config.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit rekordów per tabela (tylko do testów).",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Użyj cache zamiast pobierać.",
    )
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    args.cache.mkdir(parents=True, exist_ok=True)
    args.docs.mkdir(parents=True, exist_ok=True)

    klausimas_cache = args.cache / "klausimas.json"
    balsas_cache = args.cache / "balsas.json"

    if args.skip_fetch and klausimas_cache.exists() and balsas_cache.exists():
        print("[vilnius] using cache", file=sys.stderr)
        with open(klausimas_cache, "r", encoding="utf-8") as f:
            klausimas_rows = json.load(f)
        with open(balsas_cache, "r", encoding="utf-8") as f:
            balsas_rows = json.load(f)
    else:
        print("[vilnius] fetch Klausimas", file=sys.stderr)
        klausimas_rows = fetch_table("Klausimas", limit=args.limit)
        with open(klausimas_cache, "w", encoding="utf-8") as f:
            json.dump(klausimas_rows, f, ensure_ascii=False)

        print("[vilnius] fetch Balsas", file=sys.stderr)
        balsas_rows = fetch_table("Balsas", limit=args.limit)
        with open(balsas_cache, "w", encoding="utf-8") as f:
            json.dump(balsas_rows, f, ensure_ascii=False)

    print(f"[vilnius] {len(klausimas_rows)} klausimai, {len(balsas_rows)} balsai", file=sys.stderr)

    # Indeks: balsavimo_id → [balsas, balsas, ...]
    balsas_by_vote: dict[str, list[dict[str, str]]] = defaultdict(list)
    for b in balsas_rows:
        bid = b.get("balsavimo_id")
        if bid:
            balsas_by_vote[bid].append(b)

    # Filtruj tylko plenarkę Tarybos. Komitety lecą przez osobny scraper
    # w radoskop-premium/scrapers/komisje/vilnius.py.
    taryba_klausimai = [k for k in klausimas_rows if is_taryba(k.get("posedis", ""))]
    komitety_count = len(klausimas_rows) - len(taryba_klausimai)
    print(
        f"[vilnius] taryba={len(taryba_klausimai)} klausimai "
        f"(pominięto {komitety_count} klausimai komitetów - patrz premium scraper)",
        file=sys.stderr,
    )

    # Output: kadencja-{id}.json (Taryba only).
    kadencje_to_generate = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for kid in kadencje_to_generate:
        kadencja_def = config["kadencje"][kid]
        sessions = build_sessions(taryba_klausimai, balsas_by_vote, config, kid)

        out = {
            "id": kid,
            "label": kadencja_def.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": sessions,
        }
        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[vilnius] wrote {out_path.name}: {len(sessions)} sesji",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
