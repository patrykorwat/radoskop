#!/usr/bin/env python3
"""
Scraper Київської міської ради — format JSON-in-ZIP (per głosowanie).

Київ publikuje dane na data.gov.ua jako kwartalne archiwa ZIP.
Każdy ZIP zawiera katalog z plikami JSON, jeden per głosowanie:
  YYMMDD_N.json — NTY sesji, N-ty punkt głosowania

Struktura każdego pliku JSON:
  DocTime    — czas dokumentu (DD.MM.YYYY HH:MM:SS)
  OrgName    — "КИЇВСЬКА МІСЬКА РАДА"
  SName      — "Пленарне засіданя N сесії M скликання"
  GLType     — "ПОІМЕННЕ ГОЛОСУВАННЯ"
  GLTime     — czas głosowania (DD.MM.YYYY HH:MM:SS)
  PD_NPP     — numer punktu porządku obrad
  GL_Text    — treść pytania do głosowania
  DPList     — lista radnych: [{DPName, DPGolos}, ...]
  YESCnt     — liczba głosów Za
  NOCnt      — liczba głosów Проти
  UTRCnt     — liczba Утрималися
  NGCnt      — liczba Не голосував
  TotalCnt   — łączna obecność
  RESULT     — " РІШЕННЯ ПРИЙНЯТЕ " lub " РІШЕННЯ НЕ ПРИЙНЯТО " (padded)

DPGolos tokenы:
  "За"            → za
  "Проти"         → przeciw
  "Утримався"     → wstrzymal_sie
  "Не голосував"  → brak_glosu
  "........."     → nieobecny (nieobecny lub brak karty)

Nazwy: DPName to "Прізвище І. П." (Surname Initials.)
Niektóre rekordy mają DPName=". .. .." — nieobecni z zamaskowanym nazwiskiem.

Użycie:
  python3 scrape_kyiv.py
  python3 scrape_kyiv.py --kadencja-id 2020-2025
  python3 scrape_kyiv.py --skip-fetch
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 60
ZIP_TIMEOUT = 180

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

VOTE_TOKEN_MAP: dict[str, str] = {
    "За": "za",
    "Проти": "przeciw",
    "Утримався": "wstrzymal_sie",
    "Утрималась": "wstrzymal_sie",
    "Не голосував": "brak_glosu",
    "Не голосувала": "brak_glosu",
}
ABSENT_TOKEN = "........."

# Wzorzec dla wyniku: " РІШЕННЯ ПРИЙНЯТЕ " / " РІШЕННЯ НЕ ПРИЙНЯТО " (padded)
RESULT_ACCEPTED_RE = re.compile(r"РІШЕННЯ\s+ПРИЙНЯТЕ", re.IGNORECASE)
RESULT_REJECTED_RE = re.compile(r"РІШЕННЯ\s+НЕ\s+ПРИЙНЯТО|НЕ\s+ПРИЙНЯТО", re.IGNORECASE)

# Wzorzec daty z nazwy pliku: YYMMDD_N.json
FILENAME_DATE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d+)\.json$")


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/3 after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after 3 attempts: {last_err}")


def ckan_list_resources(dataset_id: str) -> list[dict[str, str]]:
    """Zwraca listę zasobów z CKAN data.gov.ua dla podanego dataset_id."""
    url = f"https://data.gov.ua/api/3/action/package_show?id={dataset_id}"
    print(f"  CKAN API {url}", file=sys.stderr)
    raw = http_get(url, timeout=30)
    pkg = json.loads(raw)
    if not pkg.get("success"):
        raise RuntimeError(f"CKAN error: {pkg}")
    return pkg["result"]["resources"]


def map_vote(token: str) -> str | None:
    """Mapuje token DPGolos na kategorię Radoskop."""
    t = token.strip()
    if t == ABSENT_TOKEN:
        return "nieobecny"
    if t in VOTE_TOKEN_MAP:
        return VOTE_TOKEN_MAP[t]
    # Fallback: dopasowanie case-insensitive
    tl = t.lower()
    for key, val in VOTE_TOKEN_MAP.items():
        if key.lower() == tl:
            return val
    return None


def map_result(result_str: str) -> str:
    """Mapuje RESULT na kategorię Radoskop."""
    s = result_str.strip()
    if RESULT_ACCEPTED_RE.search(s):
        return "PRZYJETE"
    if RESULT_REJECTED_RE.search(s):
        return "ODRZUCONE"
    return s  # nieznany — zostaw surowy


def parse_gltime(gl_time: str) -> str:
    """Parsuje GLTime 'DD.MM.YYYY HH:MM:SS' → 'YYYY-MM-DD'."""
    try:
        dt = datetime.strptime(gl_time.strip(), "%d.%m.%Y %H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return gl_time[:10]


def date_from_filename(fname: str) -> str:
    """Wyciąga datę z nazwy 'YYMMDD_N.json' → 'YYYY-MM-DD'."""
    m = FILENAME_DATE_RE.match(fname)
    if not m:
        return ""
    yy, mm, dd = m.group(1), m.group(2), m.group(3)
    year = f"20{yy}"
    return f"{year}-{mm}-{dd}"


def kadencja_for_date(date_str: str, kadencje: dict) -> str | None:
    if not date_str:
        return None
    sorted_kad = sorted(kadencje.items(), key=lambda kv: kv[1].get("start", ""), reverse=True)
    for kid, kdef in sorted_kad:
        if date_str >= kdef.get("start", ""):
            return kid
    return None


def parse_zip_votes(raw_zip: bytes) -> list[dict[str, Any]]:
    """Parsuje ZIP z plikami JSON. Zwraca listę wpisów per głosowanie."""
    votes: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        for zip_path in sorted(zf.namelist()):
            fname = Path(zip_path).name
            if not fname.endswith(".json"):
                continue
            try:
                raw_json = zf.read(zip_path)
                obj = json.loads(raw_json.decode("utf-8"))
            except Exception as exc:
                print(f"  WARN: skip {zip_path}: {exc}", file=sys.stderr)
                continue

            # Wyciągnij datę — z GLTime lub z nazwy pliku
            gl_time = obj.get("GLTime", "")
            date_str = parse_gltime(gl_time) if gl_time else date_from_filename(fname)

            votes.append({
                "date": date_str,
                "session_name": obj.get("SName", ""),
                "vote_type": obj.get("GLType", ""),
                "voted_at": gl_time,
                "agenda_no": obj.get("PD_NPP", ""),
                "topic": obj.get("GL_Text", ""),
                "dp_list": obj.get("DPList", []),
                "yes_cnt": int(obj.get("YESCnt", 0)),
                "no_cnt": int(obj.get("NOCnt", 0)),
                "utr_cnt": int(obj.get("UTRCnt", 0)),
                "ng_cnt": int(obj.get("NGCnt", 0)),
                "total_cnt": int(obj.get("TotalCnt", 0)),
                "result_raw": obj.get("RESULT", ""),
                "zip_filename": fname,
            })
    return votes


def build_kadencja(
    all_votes: list[dict[str, Any]],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any] | None:
    kadencje = config.get("kadencje", {})
    city_slug = config.get("slug", "kyiv")

    # Filtruj do tej kadencji
    kv = [v for v in all_votes if kadencja_for_date(v["date"], kadencje) == kadencja_id]
    if not kv:
        return None

    # Zbierz wszystkich radnych
    all_names: set[str] = set()
    for v in kv:
        for dp in v["dp_list"]:
            name = dp.get("DPName", "").strip()
            if name and name != ". .. ..":
                all_names.add(name)

    councilor_index = sorted(all_names)
    name_to_idx: dict[str, int] = {n: i for i, n in enumerate(councilor_index)}

    # Grupuj głosowania per sesja (unikalny klucz: date + session_name)
    sessions_map: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for v in kv:
        key = (v["date"], v["session_name"])
        sessions_map[key].append(v)

    sessions_out: list[dict[str, Any]] = []
    for (date_str, sess_name), svotes in sorted(sessions_map.items()):
        present: set[str] = set()
        for v in svotes:
            for dp in v["dp_list"]:
                name = dp.get("DPName", "").strip()
                golos = dp.get("DPGolos", "").strip()
                if name and name != ". .. .." and golos != ABSENT_TOKEN:
                    present.add(name)
        attendees = sorted(present)
        sessions_out.append({
            "date": date_str,
            "number": date_str,
            "title": sess_name,
            "vote_count": len(svotes),
            "attendee_count": len(attendees),
            "attendees": attendees,
            "source_url": "",
        })

    # Buduj votes[]
    votes_out: list[dict[str, Any]] = []
    for v in sorted(kv, key=lambda x: (x["date"], x.get("agenda_no", ""))):
        result_cat = map_result(v["result_raw"])

        counts: dict[str, int] = {c: 0 for c in CATEGORIES}
        named_votes_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}

        for dp in v["dp_list"]:
            name = dp.get("DPName", "").strip()
            golos = dp.get("DPGolos", "").strip()
            cat = map_vote(golos)
            if not cat:
                continue
            bucket = "nieobecni" if cat == "nieobecny" else cat
            counts[bucket] += 1
            if name and name in name_to_idx:
                named_votes_idx[bucket].append(name_to_idx[name])

        for bucket in named_votes_idx:
            named_votes_idx[bucket].sort()

        vote_id = f"{city_slug}_{v['date']}_{v['agenda_no']}_{v['zip_filename'].replace('.json', '')}"

        votes_out.append({
            "id": vote_id,
            "session_date": v["date"],
            "session_number": v["date"],
            "source_url": "",
            "topic": v["topic"],
            "druk": v["agenda_no"],
            "resolution": "",
            "result": result_cat,
            "result_native": v["result_raw"].strip(),
            "counts": counts,
            "named_votes": named_votes_idx,
            "voted_at": v["voted_at"],
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
    cache_index = args.cache / "resources.json"
    all_votes_cache = args.cache / "all_votes.json"

    # Lista zasobów z CKAN
    if args.skip_fetch and cache_index.exists():
        print("[kyiv] using cached resource list", file=sys.stderr)
        with open(cache_index, encoding="utf-8") as f:
            resources = json.load(f)
    else:
        resources = ckan_list_resources(dataset_id)
        with open(cache_index, "w", encoding="utf-8") as f:
            json.dump(resources, f, ensure_ascii=False, indent=2)

    # Filtruj do JSON/ZIP zasobów (pomijaj PDF, XLSX, stare zasoby pre-2023)
    zip_resources = [
        r for r in resources
        if r.get("url", "").endswith(".zip")
        and r.get("format", "").lower() in ("json, zip", "jason, zip", "zip", "json,zip")
    ]
    print(f"[kyiv] {len(zip_resources)} zasobów ZIP do przetworzenia", file=sys.stderr)

    # Pobierz i sparsuj wszystkie ZIPy
    if args.skip_fetch and all_votes_cache.exists():
        print("[kyiv] using cached all_votes", file=sys.stderr)
        with open(all_votes_cache, encoding="utf-8") as f:
            all_votes = json.load(f)
    else:
        all_votes: list[dict[str, Any]] = []
        for i, res in enumerate(zip_resources, 1):
            url = res["url"]
            name = res.get("name", url)
            zip_cache = args.cache / f"zip_{i:03d}.zip"
            if zip_cache.exists() and args.skip_fetch:
                raw = zip_cache.read_bytes()
            else:
                print(f"[kyiv] [{i}/{len(zip_resources)}] {name[:60]}", file=sys.stderr)
                try:
                    raw = http_get(url, timeout=ZIP_TIMEOUT)
                    zip_cache.write_bytes(raw)
                except RuntimeError as exc:
                    print(f"  WARN: skip ZIP: {exc}", file=sys.stderr)
                    continue
            votes = parse_zip_votes(raw)
            all_votes.extend(votes)

        with open(all_votes_cache, "w", encoding="utf-8") as f:
            json.dump(all_votes, f, ensure_ascii=False, indent=2)

    print(f"[kyiv] {len(all_votes)} głosowań łącznie", file=sys.stderr)

    kadencje_to_build = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    # Usuń stare kadencja-*.json
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            try:
                old.unlink()
            except OSError:
                pass

    for kid in kadencje_to_build:
        print(f"[kyiv] budowanie kadencja-{kid}", file=sys.stderr)
        built = build_kadencja(all_votes, config, kid)
        if built is None or not built.get("votes"):
            print(f"[kyiv] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue

        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(built, f, ensure_ascii=False, indent=2)

        print(
            f"[kyiv] napisano {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
