#!/usr/bin/env python3
"""
Scraper głosowań Pražského zastupitelstva — wersja open-data CSV.

Praha publikuje oficjalny dataset z głosowaniami w katalogu otwartych
danych (LKOD / data.gov.cz). Plik CSV jest hostowany na storage.golemio.cz
(nie chroniony przez F5 ASM WAF, w przeciwieństwie do praha.eu/o/prg/).

Endpoint:
    https://storage.golemio.cz/ckan/obis/Vysledky_hlasovani_ZHMP_<YYYY-YYYY>.csv

Schema CSV (kolumny):
    cislotisku, cislousneseni, volobd, datumjednani, orgjednotka,
    rokusneseni, nazevtisku, predkladatel, poradi, cislojednani,
    datumcas, kbodu, pritomno, nepritomno, pocetpro, pocetproti, pocetzdrzel,
    {"Nazwisko Imię Tytuł"}*65   ← wartości "Hlas pro|Hlas proti|Zdržel se|Nehlasoval|Chyběl"

Mapowanie do Radoskop:
    Hlas pro      → za
    Hlas proti    → przeciw
    Zdržel se     → wstrzymal_sie
    Nehlasoval    → brak_glosu
    Chyběl        → nieobecni

Skrypt:
1. Czyta config.json (kadencje, vote_text_map_csv).
2. Pobiera CSV (1 request, ~1.7 MB).
3. Iteruje wiersze, dla każdego buduje "głosowanie" w schemacie Radoskop.
4. Grupuje po dacie sesji → sessions[].
5. Buduje councilor_index z nagłówków kolumn (po normalize).
6. Zapisuje docs/kadencja-{kadencja_id}.json.

Plus: nie używa F5 ASM-blocked endpoints, więc działa też z domowego IP.

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --kadencja-id 2018-2022
    python3 scrape_glosowania.py --csv-url https://...
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
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

# Domyślne URL-e CSV per kadencja. Pochodzą z LKOD katalogu organizacji
# Hl. m. Prahy (api.lkod.cz/lod/03bdf7d6.../catalog/<id>). Można też
# wyciągnąć dynamicznie z LKOD API, ale stałe URL-e są stabilne i prosta.
DEFAULT_CSV_URLS = {
    "2022-2026": "https://storage.golemio.cz/ckan/obis/Vysledky_hlasovani_ZHMP_2022_-_2026.csv",
    "2018-2022": "https://storage.golemio.cz/ckan/obis/Vysledky_hlasovani_ZHMP_2018_-_2022.csv",
    "2014-2018": "https://storage.golemio.cz/ckan/obis/Vysledky_hlasovani_ZHMP_2014_-_2018.csv",
    "2010-2014": "https://storage.golemio.cz/ckan/obis/Vysledky_hlasovani_ZHMP_2010_-_2014.csv",
}

# Mapowanie wartości głosu z CSV do schematu Radoskop.
VOTE_TEXT_TO_CATEGORY = {
    "Hlas pro": "za",
    "Hlas proti": "przeciw",
    "Zdržel se": "wstrzymal_sie",
    "Nehlasoval": "brak_glosu",
    "Chyběl": "nieobecni",
}

# Stałe kategorie do counts/named_votes. Kolejność zachowana.
CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")


def http_download(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Pobiera URL do pliku. Bez WAF, bez retry — host jest niezależny."""
    print(f"  GET {url}", file=sys.stderr)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,application/octet-stream,*/*",
    })
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"  saved {len(data)/1024:.1f} KB → {dest}", file=sys.stderr)


def normalize_name(full: str) -> str:
    """Z headera CSV: "Arnotová  Kateřina Mgr. " → "Kateřina Arnotová".

    Format header: NAZWISKO  IMIĘ tytuły... (pojedyncze podwójne spacje
    między nazwiskiem a imieniem, plus titule po imieniu, plus trailing space).
    """
    s = full.strip().rstrip(",").rstrip()
    titles = {
        "Mgr", "Ing", "MUDr", "MVDr", "PhDr", "JUDr", "RNDr",
        "Bc", "BcA", "MgA", "doc", "prof", "PaedDr", "ThDr",
        "Dr", "Ph", "PhD", "CSc", "MBA", "MSc", "DiS", "DrSc",
        "arch", "et", "M", "A", "LL", "D", "h",
    }
    out = []
    for t in s.split():
        clean = t.rstrip(",").rstrip(".")
        clean_no_dots = clean.replace(".", "")
        if clean_no_dots in titles or clean.lower() in (x.lower() for x in titles):
            continue
        if all(p in titles or p == "" for p in clean.split(".")):
            continue
        out.append(t.rstrip(","))

    # Po stripie tytułów: NAZWISKO IMIĘ. CSV header ma nazwisko jako pierwsze.
    # Odwracamy tylko jeśli mamy dokładnie 2 tokeny (typowy przypadek).
    # Dla nazwisk dwuczłonowych (np. "Kordová Marvanová Hana") trzymamy
    # imię na końcu (ostatni token) i resztę traktujemy jako nazwisko.
    if len(out) >= 2:
        # Heurystyka: ostatni token to imię, reszta to nazwisko.
        # CSV format: "Nazwisko [Drugiečłon] Imię tytuły".
        first_name = out[-1]
        last_name = " ".join(out[:-1])
        return f"{first_name} {last_name}"
    return " ".join(out)


def slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s\-]", "", ascii_only.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "councilor"


def parse_date_iso(czech_dt: str) -> str | None:
    """Czech "03.11.2022 0:00:00" → ISO "2022-11-03"."""
    s = czech_dt.strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def parse_full_iso(czech_iso: str) -> str | None:
    """Pole datumcas: "2022-11-03T14:45:06" → "2022-11-03"."""
    s = czech_iso.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def parse_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def build_kadencja(
    config: dict[str, Any],
    kadencja_id: str,
    csv_path: Path,
    api_base: str = "https://praha.eu",
) -> dict[str, Any]:
    """Czyta CSV i buduje kadencja-{id}.json."""

    # Wczytaj wszystkie linie. Plik ma trailing tabs/whitespace w wartościach,
    # więc trim po stripowaniu.
    print(f"  parse CSV {csv_path}", file=sys.stderr)
    text = csv_path.read_text(encoding="utf-8")
    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if not rows:
        raise ValueError("empty CSV")

    header = [c.rstrip() for c in rows[0]]

    # Stałe meta-kolumny (do indeksu kbodu = "k bodu" przed kolumnami głosów).
    meta_cols = [
        "cislotisku", "cislousneseni", "volobd", "datumjednani",
        "orgjednotka", "rokusneseni", "nazevtisku", "predkladatel",
        "poradi", "cislojednani", "datumcas", "kbodu",
        "pritomno", "nepritomno", "pocetpro", "pocetproti", "pocetzdrzel",
    ]
    # Validacja
    if header[:len(meta_cols)] != meta_cols:
        raise ValueError(f"unexpected CSV header: {header[:len(meta_cols)]}")

    # Kolumny zastupitelů zaczynają się od indeksu 17.
    councilor_cols = header[len(meta_cols):]
    # Ostatnia kolumna często to "neurčeno" (placeholder) lub pusta — zostawiamy
    # ją jako część listy, ale filtrujemy podczas budowy councilor_index.
    councilor_index: list[str] = []
    col_idx_to_canonical: dict[int, int] = {}  # kolumna CSV → indeks w councilor_index
    for i, col in enumerate(councilor_cols):
        col_clean = col.strip()
        if not col_clean or col_clean.lower() in ("neurčeno", "neurceno"):
            continue
        canonical = normalize_name(col_clean)
        if not canonical:
            continue
        if canonical in councilor_index:
            cidx = councilor_index.index(canonical)
        else:
            cidx = len(councilor_index)
            councilor_index.append(canonical)
        col_idx_to_canonical[i] = cidx

    print(f"  councilors detected: {len(councilor_index)}", file=sys.stderr)

    sessions_acc: dict[str, dict[str, Any]] = {}
    votes_out: list[dict[str, Any]] = []
    skipped = 0

    for row in rows[1:]:
        # Pad row
        while len(row) < len(header):
            row.append("")
        meta = dict(zip(meta_cols, [row[i] for i in range(len(meta_cols))]))
        votes_raw = row[len(meta_cols):]

        date_iso = parse_full_iso(meta.get("datumcas", "")) or parse_date_iso(
            meta.get("datumjednani", "")
        )
        if not date_iso:
            skipped += 1
            continue

        # Buduj named_votes per kategoria.
        named: dict[str, list[int]] = {c: [] for c in CATEGORIES}
        for col_i, val in enumerate(votes_raw):
            v = val.strip()
            if not v:
                continue
            cat = VOTE_TEXT_TO_CATEGORY.get(v)
            if cat is None:
                continue
            cidx = col_idx_to_canonical.get(col_i)
            if cidx is None:
                continue
            named[cat].append(cidx)

        counts = {c: len(named[c]) for c in CATEGORIES}

        # Wynik głosowania — w CSV nie ma flagi pass/fail, ale możemy
        # zrekonstruować z liczb: pritomno/2 < pocetpro → PRZYJETE.
        pritomno = parse_int(meta.get("pritomno", "")) or 0
        pocetpro = parse_int(meta.get("pocetpro", "")) or 0
        if pritomno > 0 and pocetpro * 2 > pritomno:
            result = "PRZYJETE"
        elif pritomno > 0:
            result = "ODRZUCONE"
        else:
            result = ""

        # Topic: większość rekordów Pragi ma puste nazevtisku, kbodu zawiera
        # tylko "usnesení k Z-XXXXX". Jeśli mamy nazevtisku → użyj. Jeśli nie,
        # spróbuj wzbogacić: "{predkladatel} → {kbodu}". Predkladatel sam
        # pomaga kontekstowo (Rada HMP, primátor, konkretny zastupitel).
        nazev = (meta.get("nazevtisku") or "").strip()
        kbodu = (meta.get("kbodu") or "").strip().rstrip()
        predkladatel = (meta.get("predkladatel") or "").strip()
        if nazev:
            topic = nazev
        elif kbodu and predkladatel:
            topic = f"{predkladatel}: {kbodu}"
        else:
            topic = kbodu or predkladatel or ""

        druk = (meta.get("cislotisku") or "").strip() or None
        resolution = (meta.get("cislousneseni") or "").strip() or None
        meeting_num = (meta.get("cislojednani") or "").strip()

        # Resolution często ma slash (np. "1/2"), co psuje URL routing
        # /glosowanie/{id}/. Slash w slug zostanie zinterpretowany jako
        # separator ścieżki i SPA nie znajdzie głosowania. Zamieniamy na "-".
        resolution_safe = resolution.replace("/", "-") if resolution else ""

        # Sesja: bucketujemy po dacie. Numer sesji to cislojednani.
        sess_key = date_iso
        if sess_key not in sessions_acc:
            sessions_acc[sess_key] = {
                "date": date_iso,
                "number": meeting_num,
                "vote_count": 0,
                "attendees": set(),
                "source_url": (
                    f"{api_base}/vysledky-hlasovani#/?periodId={config.get('praha_period_id', '')}"
                    f"&meetingNumber={meeting_num}"
                    if meeting_num else f"{api_base}/vysledky-hlasovani"
                ),
            }
        sessions_acc[sess_key]["vote_count"] += 1
        # Attendees: każdy obecny (głosował lub Nehlasoval = obecny ale wstrzymał
        # głos). Chyběl → nieobecni → out.
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for ci in named[cat]:
                sessions_acc[sess_key]["attendees"].add(councilor_index[ci])

        vote_id_base = f"praha_{druk or ''}_{resolution_safe}".strip("_")
        votes_out.append({
            "id": vote_id_base or f"praha_{len(votes_out)}",
            "session_date": date_iso,
            "session_number": meeting_num,
            "source_url": (
                f"{api_base}/vysledky-hlasovani#/?periodId={config.get('praha_period_id', '')}"
                f"&meetingNumber={meeting_num}&resolutionNumber={resolution or ''}"
            ),
            "topic": topic,
            "druk": druk,
            "resolution": resolution,
            "result": result,
            "counts": counts,
            "named_votes": named,
            "voted_at": date_iso,
        })

    if skipped:
        print(f"  skipped {skipped} rows without parsable date", file=sys.stderr)

    # Finalizacja sessions.
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
    parser.add_argument("--csv-url", default=None,
                        help="URL CSV. Default: DEFAULT_CSV_URLS dla danej kadencji.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE),
                        help="Katalog cache na pobrany CSV.")
    parser.add_argument("--output", default=None)
    parser.add_argument("--no-cache", action="store_true",
                        help="Zignoruj cache, zawsze pobierz świeży CSV.")
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

    csv_url = args.csv_url or DEFAULT_CSV_URLS.get(kadencja_id)
    if not csv_url:
        print(f"[scrape] brak CSV URL dla kadencji {kadencja_id}", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    csv_path = cache_dir / f"vysledky_hlasovani_zhmp_{kadencja_id}.csv"

    if args.no_cache or not csv_path.is_file():
        try:
            http_download(csv_url, csv_path)
        except (HTTPError, URLError) as exc:
            print(f"[scrape] download fail: {exc}", file=sys.stderr)
            return 2
    else:
        print(f"  cached CSV: {csv_path}", file=sys.stderr)

    api_base = config.get("praha_api_base", "https://praha.eu")
    out = build_kadencja(config, kadencja_id, csv_path, api_base=api_base)

    output_path = Path(args.output) if args.output else DEFAULT_DOCS / f"kadencja-{kadencja_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] zapisano {output_path}", file=sys.stderr)
    print(f"  sesje: {out['total_sessions']}", file=sys.stderr)
    print(f"  głosowania: {out['total_votes']}", file=sys.stderr)
    print(f"  zastupitelé: {out['total_councilors']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
