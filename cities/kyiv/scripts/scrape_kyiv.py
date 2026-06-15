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
import zipfile
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


# GL_Text to jedyne pole wpisane ręcznie — bywa zepsute: surowe znaki
# kontrolne (raw \n, \t → "Invalid control character") albo niezescapowane
# proste cudzysłowy w nazwach ustaw/programów (→ "Expecting ',' delimiter").
# Reszta pliku (DPList itd.) jest generowana maszynowo i poprawna, więc
# naprawiamy tylko wnętrze GL_Text, kotwicząc na następnym polu "DPList".
GLTEXT_RE = re.compile(r'("GL_Text"\s*:\s*")(.*?)("\s*,\s*"DPList")', re.DOTALL)


def lenient_loads(raw: str) -> dict[str, Any]:
    """Parsuje JSON głosowania, naprawiając typowe uszkodzenia pola GL_Text."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 1) surowe znaki kontrolne w stringach (raw \n / \t w GL_Text)
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        pass
    # 2) niezescapowane cudzysłowy w GL_Text — przepisz wnętrze i zescapuj
    m = GLTEXT_RE.search(raw)
    if not m:
        raise
    fixed_val = json.dumps(m.group(2), ensure_ascii=False)
    repaired = raw[:m.start()] + '"GL_Text": ' + fixed_val + ', "DPList"' + raw[m.end():]
    return json.loads(repaired, strict=False)


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
                obj = lenient_loads(raw_json.decode("utf-8"))
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


def _name_key(name: str) -> str:
    """Klucz tożsamości radnego odporny na warianty zapisu DPName.

    DPName ma format "Прізвище І. П." ale źródło jest niespójne: bywa
    podwójna spacja ("Омельченко  О. О."), brak spacji po kropce inicjału
    ("Ільницький С.В." vs "Ільницький С. В."). Usunięcie WSZYSTKICH spacji +
    casefold łączy te warianty. Dwie różne osoby nie różnią się tylko spacjami
    (inicjały po-batькові zostają, więc "Левін В. І." != "Левін О. І.").
    Literówki w nazwisku (Домагальський/Домогальський) obsługuje name_aliases.
    """
    return re.sub(r"\s+", "", name).casefold()


def build_canonical_map(
    all_votes: list[dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, str]:
    """Mapa surowy_DPName -> kanoniczny_DPName.

    Stosuje jawne aliasy z configu, grupuje warianty po _name_key, kanoniczna
    = najczęstsza forma (remis: dłuższa/spacjowana, potem leksykalnie).
    """
    from collections import Counter
    freq: Counter = Counter()
    for v in all_votes:
        for dp in v["dp_list"]:
            n = dp.get("DPName", "").strip()
            if n and n != ". .. ..":
                freq[aliases.get(n, n)] += 1
    groups: dict[str, list[str]] = defaultdict(list)
    for n in freq:
        groups[_name_key(n)].append(n)
    canon_of_target: dict[str, str] = {}
    for _key, names in groups.items():
        best = max(names, key=lambda n: (freq[n], len(n), n))
        # Forma wyświetlana: pojedyncze spacje (źródło bywa daje "Прізвище  І.П.").
        best_display = re.sub(r"\s+", " ", best).strip()
        for n in names:
            canon_of_target[n] = best_display
    result: dict[str, str] = {}
    raw_names = {
        dp.get("DPName", "").strip()
        for v in all_votes for dp in v["dp_list"]
    }
    for r in raw_names:
        target = aliases.get(r, r)
        result[r] = canon_of_target.get(target, target)
    return result


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

    # Kanonikalizacja DPName: źródło zapisuje to samo nazwisko na kilka sposobów
    # (podwójne spacje, "С.В." vs "С. В."), co nadmuchiwało councilor_index
    # (265 zamiast 120 mandatów + rotacja). Łączymy warianty; literówki nazwisk
    # z config["name_aliases"].
    canon = build_canonical_map(kv, config.get("name_aliases", {}))

    def _cn(raw: str) -> str:
        return canon.get(raw, raw)

    # Zbierz wszystkich radnych (po kanonikalizacji)
    all_names: set[str] = set()
    for v in kv:
        for dp in v["dp_list"]:
            name = dp.get("DPName", "").strip()
            if name and name != ". .. ..":
                all_names.add(_cn(name))

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
                    present.add(_cn(name))
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
            cname = _cn(name)
            if cname and cname in name_to_idx:
                named_votes_idx[bucket].append(name_to_idx[cname])

        for bucket in named_votes_idx:
            named_votes_idx[bucket] = sorted(set(named_votes_idx[bucket]))

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
    ckan_timeout = int(config.get("ckan_timeout", 30))
    cache_index = args.cache / "resources.json"
    all_votes_cache = args.cache / "all_votes.json"
    zips_dir = args.cache / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.cache / "zip_manifest.json"

    # Tryb w pełni offline: użyj zagregowanego all_votes.json bez dotykania sieci.
    if args.skip_fetch and all_votes_cache.exists():
        print("[kyiv] using cached all_votes (offline)", file=sys.stderr)
        with open(all_votes_cache, encoding="utf-8") as f:
            all_votes = json.load(f)
        print(f"[kyiv] {len(all_votes)} głosowań łącznie", file=sys.stderr)
    else:
        # Lista zasobów z CKAN (tania — jeden request). Przy 403/timeout
        # spada na dyskowy cache resources.json zamiast ubijać run: ZIPy i tak
        # są zwykle już w cache, więc da się dokończyć z ostatniej znanej listy.
        resources, stale = ckan_resources_with_cache(
            dataset_id,
            cache_path=cache_index,
            skip_fetch=args.skip_fetch,
            timeout=ckan_timeout,
            label="kyiv",
        )
        if resources is None:
            return 1
        if stale:
            print("[kyiv] UWAGA: lista zasobów z cache (data.gov.ua odrzuciło żądanie)", file=sys.stderr)

        # Filtruj do JSON/ZIP zasobów (pomijaj PDF, XLSX, stare zasoby pre-2023)
        zip_resources = [
            r for r in resources
            if r.get("url", "").endswith(".zip")
            and r.get("format", "").lower() in ("json, zip", "jason, zip", "zip", "json,zip")
        ]
        print(f"[kyiv] {len(zip_resources)} zasobów ZIP do przetworzenia", file=sys.stderr)

        # Cache inkrementalny: ZIPy to per-sesyjne archiwa, historyczne są
        # NIEZMIENNE (last_modified z 2022). Bez tego pipeline ściągał codziennie
        # wszystkie 48 archiwów (~25 min). Klucz świeżości = id + last_modified
        # zasobu CKAN; pobieramy tylko nowe/zmienione, resztę czytamy z dysku.
        # ZIP nazwany stabilnym resource id (kolejność zasobów bywa zmienna).
        manifest: dict[str, str] = {}
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError):
                manifest = {}

        def _fresh_key(res: dict[str, Any]) -> str:
            return str(
                res.get("last_modified")
                or res.get("revision_id")
                or res.get("size")
                or ""
            )

        all_votes: list[dict[str, Any]] = []
        fetched = reused = skipped = 0
        for i, res in enumerate(zip_resources, 1):
            url = res["url"]
            name = res.get("name", url)
            rid = res.get("id") or re.sub(r"\W+", "_", url)[-60:]
            fresh = _fresh_key(res)
            zip_cache = zips_dir / f"{rid}.zip"

            cache_ok = zip_cache.exists() and (
                manifest.get(rid) == fresh or args.skip_fetch
            )
            if cache_ok:
                raw = zip_cache.read_bytes()
                reused += 1
            else:
                print(f"[kyiv] [{i}/{len(zip_resources)}] pobieram {name[:55]}", file=sys.stderr)
                try:
                    raw = http_get(url, timeout=ZIP_TIMEOUT)
                except RuntimeError as exc:
                    print(f"  WARN: skip ZIP: {exc}", file=sys.stderr)
                    skipped += 1
                    continue
                zip_cache.write_bytes(raw)
                manifest[rid] = fresh
                fetched += 1

            all_votes.extend(parse_zip_votes(raw))

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        with open(all_votes_cache, "w", encoding="utf-8") as f:
            json.dump(all_votes, f, ensure_ascii=False, indent=2)

        print(
            f"[kyiv] ZIP cache: {reused} z dysku, {fetched} pobrane, {skipped} pominięte",
            file=sys.stderr,
        )
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

    all_council_names: set[str] = set()
    for kid in kadencje_to_build:
        print(f"[kyiv] budowanie kadencja-{kid}", file=sys.stderr)
        built = build_kadencja(all_votes, config, kid)
        if built is None or not built.get("votes"):
            print(f"[kyiv] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue

        all_council_names.update(built["councilor_index"])

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

    # club_assignments z faction_roster (kmr.gov.ua, lista wg фракцій).
    # Głosy nie zawierają frakcji, a kmr.gov.ua/profiles jest JS + WAF, więc
    # roster trzymamy w config (nazwiska w formacie "Прізвище І. П."). Dopasowanie
    # do nazw z głosowań odporne na spacje/kropki przez _name_key. Piszemy
    # docs/club_assignments.json (build_assembly_metrics czyta stąd kluby).
    roster = config.get("faction_roster", {})
    if roster and all_council_names:
        roster_by_key = {_name_key(k): v for k, v in roster.items()}
        assignments: dict[str, str] = {}
        for name in sorted(all_council_names):
            slug = roster_by_key.get(_name_key(name))
            if slug:
                assignments[name] = slug
        ca_path = args.docs / "club_assignments.json"
        with open(ca_path, "w", encoding="utf-8") as f:
            json.dump(assignments, f, ensure_ascii=False, indent=2)
        print(
            f"[kyiv] napisano club_assignments.json: "
            f"{len(assignments)}/{len(all_council_names)} radnych z klubem",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
