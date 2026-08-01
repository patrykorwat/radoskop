#!/usr/bin/env python3
"""
Scraper Szeged Megyei Jogú Város Közgyűlésének szavazásai a TimPortal
rendszerből (eservices.szeged.eu).

ŹRÓDŁO DANYCH I DLACZEGO DZIAŁA
================================
Szeged publikuje protokoły (jegyzőkönyv) posiedzeń rady jako PDF z warstwą
tekstową. System głosowania elektronicznego drukuje na końcu protokołu dla
KAŻDEJ uchwały blok "Szavazás eredménye" z imienną tabelą Név/Voks/Frakció,
czyli nazwisko, głos i frakcja każdego radnego.

Szeged używa systemu TimPortal (Angular + PHP backend) z API REST pod
tim_public/. W przeciwieństwie do Budapesztu, Szeged nie ma osobnego API
do listy sesji — dane są dostępne przez endpointy hatarozatok_list.php
i jegyzokonyvek_list.php.

PIPELINE
========
1. GET listy komisji/posiedzeń z hatarozatok_list.php
2. Pobierz PDF z jegyzőkönyv (download_public_file.php?guid=...)
3. pdftotext -> tekst, split na bloki "Szavazás eredménye"
4. Per blok wyciągnij: Száma (id), Ideje (data+czas), Típusa, Határozat
   (wynik), Tárgya (temat), agregaty oraz imienną tabelę Név/Voks/Frakció
5. Zbuduj docs/kadencja-{id}.json (schema jak Budapeszt)

Mapowanie głosu (Voks) -> kategoria Radoskop:
   Igen           -> za
   Nem            -> przeciw
   Tartózkodik / Tart.  -> wstrzymal_sie
   Nem szavazott / Nem szav.  -> brak_glosu
   Távol          -> nieobecni

Wymaga zewn. tooli:
- pdftotext (poppler-utils)

Użycie:
    python3 scrape_szeged.py
    python3 scrape_szeged.py --max-sessions 2
    python3 scrape_szeged.py --skip-fetch
    python3 scrape_szeged.py --pdf /sciezka/jegyzokonyv.pdf
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
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
TIMEOUT = 90
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.3

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Voks (wartość głosu w tabeli imiennej) -> kategoria Radoskop.
VOKS_TO_CATEGORY = {
    "Igen": "za",
    "Nem": "przeciw",
    "Tartózkodik": "wstrzymal_sie",
    "Tart.": "wstrzymal_sie",
    "Tart": "wstrzymal_sie",
    "Nem szavazott": "brak_glosu",
    "Nem szav.": "brak_glosu",
    "Nem szav": "brak_glosu",
    "Nemszav.": "brak_glosu",
    "Távol": "nieobecni",
}

VOKS_TOKENS = [
    "Nem szavazott",
    "Nem szav.",
    "Nemszav.",
    "Tartózkodik",
    "Tart.",
    "Igen",
    "Távol",
    "Nem",
]

_VOKS_ALT = "|".join(re.escape(t) for t in VOKS_TOKENS)
MEMBER_ROW_RE = re.compile(
    rf"^\s*(?P<name>.+?)\s+(?P<voks>{_VOKS_ALT})(?:\s+|(?=[A-ZÁÉÍÓÖŐÚÜŰ]))(?P<frakcio>\S.*?)\s*$"
)

NAME_HEADER_RE = re.compile(r"N[ée]v\s+Voks\s+Frakci[óo]", re.IGNORECASE)
BLOCK_SPLIT_TOKEN = "Szavazás eredménye"

HU_MONTHS = {
    "január": "01", "február": "02", "március": "03", "április": "04",
    "május": "05", "június": "06", "július": "07", "augusztus": "08",
    "szeptember": "09", "október": "10", "november": "11", "december": "12",
}


def _cache_key(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get_text(url: str, cache_dir: Path | None) -> str:
    cache_file: Path | None = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.html"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*"})
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if cache_file:
                cache_file.write_text(raw, encoding="utf-8")
            time.sleep(SLEEP_BETWEEN_CALLS)
            return raw
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {RETRY_COUNT} attempts: {last_err}")


def http_post_json(url: str, data: dict[str, str], cache_dir: Path | None) -> dict[str, Any]:
    """POST JSON body, return parsed JSON response."""
    cache_file: Path | None = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url + json.dumps(data, sort_keys=True))}.json"
        if cache_file.is_file():
            return json.loads(cache_file.read_text(encoding="utf-8"))

    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json,*/*",
    })
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if cache_file:
                cache_file.write_text(raw, encoding="utf-8")
            time.sleep(SLEEP_BETWEEN_CALLS)
            return parsed
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"POST failed after {RETRY_COUNT} attempts: {last_err}")


def http_download(url: str, target: Path) -> bool:
    if target.is_file() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                target.write_bytes(resp.read())
            time.sleep(SLEEP_BETWEEN_CALLS)
            return True
        except (HTTPError, URLError, TimeoutError) as exc:
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    return False


def have_command(name: str) -> bool:
    return shutil.which(name) is not None


def pdf_to_text(pdf_path: Path) -> str:
    """pdftotext -layout -> tekst."""
    if not have_command("pdftotext"):
        print("UWAGA: brak pdftotext (zainstaluj poppler-utils)", file=sys.stderr)
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            check=True, capture_output=True, timeout=120, text=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  pdftotext failed: {exc}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Parser bloku "Szavazás eredménye" (identyczny jak w Budapeszcie)
# ---------------------------------------------------------------------------

def parse_ideje(line: str) -> str:
    m = re.search(
        r"Ideje:\s*(\d{4})\.?\s+([A-Za-zíáéúőóüöÍÁÉÚŐÓÜÖ]+)\.?\s+(\d{1,2})\.?\s+(\d{1,2}):(\d{2})",
        line,
    )
    if not m:
        return ""
    year, month_name, day, hh, mm = m.groups()
    month = HU_MONTHS.get(month_name.lower())
    if not month:
        return ""
    return f"{year}-{month}-{int(day):02d}T{int(hh):02d}:{int(mm):02d}:00"


def _clean_frakcio(raw: str) -> str:
    f = re.sub(r"\s+", " ", raw).strip()
    f = re.sub(r"\s*[\d.,%]+\s*$", "", f).strip()
    return f


def parse_member_rows(lines: list[str]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        if re.match(r"^\s*\d+\s+Száma:", line):
            continue
        m = MEMBER_ROW_RE.match(line)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group("name")).strip()
        voks_raw = m.group("voks").strip()
        cat = VOKS_TO_CATEGORY.get(voks_raw)
        if cat is None:
            cat = VOKS_TO_CATEGORY.get(voks_raw.rstrip("."))
        if cat is None:
            continue
        frakcio = _clean_frakcio(m.group("frakcio"))
        if not name or not frakcio:
            continue
        out.append((name, cat, frakcio))
    return out


def parse_vote_block(chunk: str) -> dict[str, Any] | None:
    lines = chunk.splitlines()
    szama = ""
    ideje = ""
    tipus = ""
    result_native = ""
    topic = ""

    name_header_idx = None
    for i, line in enumerate(lines):
        if not szama:
            m = re.search(r"Száma:\s*([0-9A-Za-z./\-]+)", line)
            if m:
                szama = m.group(1).strip()
        if not ideje and "Ideje:" in line:
            ideje = parse_ideje(line)
        if not tipus:
            m = re.search(r"Típusa:\s*([A-Za-zíáéúőóüöÍÁÉÚŐÓÜÖ]+)", line)
            if m:
                tipus = m.group(1).strip()
        if not result_native:
            m = re.search(r"Határozat[;:]\s*([A-Za-zíáéúőóüöÍÁÉÚŐÓÜÖ]+)", line)
            if m:
                result_native = m.group(1).strip()
        if not topic:
            m = re.search(r"Tárgya:\s*(.+?)\s*$", line)
            if m:
                topic = m.group(1).strip()
        if name_header_idx is None and NAME_HEADER_RE.search(line):
            name_header_idx = i

    if name_header_idx is None:
        return None

    members = parse_member_rows(lines[name_header_idx + 1:])
    if not members:
        return None

    return {
        "szama": szama,
        "voted_at": ideje,
        "session_date": ideje[:10] if ideje else "",
        "tipus": tipus,
        "result_native": result_native,
        "topic": topic,
        "members": members,
    }


def parse_jegyzokonyv_text(text: str) -> list[dict[str, Any]]:
    if BLOCK_SPLIT_TOKEN not in text:
        return []
    parts = text.split(BLOCK_SPLIT_TOKEN)
    blocks: list[dict[str, Any]] = []
    for part in parts[1:]:
        parsed = parse_vote_block(part)
        if parsed:
            blocks.append(parsed)
    return blocks


# ---------------------------------------------------------------------------
# API Szeged
# ---------------------------------------------------------------------------

def get_sessions_from_api(api_base: str, year: int, cache: Path | None) -> list[dict[str, Any]]:
    """Pobiera listę posiedzeń Közgyűlés dla danego roku z API hatarozatok_list.

    Zwraca listę dictów: {date, title, guid, ules_id}
    """
    url = f"{api_base}/hatarozatok/hatarozatok_list.php"
    payload = {"data": json.dumps({"ev": year, "testulet": "kgy", "submenu": "GY"})}
    resp = http_post_json(url, payload, cache)

    sessions: dict[str, dict[str, Any]] = {}
    for item in resp.get("list", []):
        files = item.get("files", {})
        if not files:
            continue
        label = item.get("label", "")
        # Extract date from label like "Közgyűlés / 2026-06-26"
        date_m = re.search(r"(\d{4})-(\d{2})-(\d{2})", label)
        if not date_m:
            continue
        date = f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}"

        # Parse file info: filename.pdf¤uuid¤type
        nyilt = files.get("nyilt", "")
        parts = nyilt.split("¤")
        if len(parts) >= 2:
            guid = parts[1]
            ules_id = item.get("name", "")
            if date not in sessions:
                sessions[date] = {
                    "date": date,
                    "title": label,
                    "guid": guid,
                    "ules_id": ules_id,
                }
    return sorted(sessions.values(), key=lambda s: s["date"])


# ---------------------------------------------------------------------------
# Budowanie kadencji
# ---------------------------------------------------------------------------

def normalize_result(result_native: str, result_map: dict[str, str]) -> str:
    if not result_native:
        return ""
    for key, mapped in result_map.items():
        if key.lower() in result_native.lower():
            return mapped
    return result_native


_HONORIFICS = {"dr", "dr.", "prof", "prof.", "ifj", "ifj.", "id", "id.", "özv", "özv."}


def _strip_honorifics(name: str) -> str:
    parts = name.split()
    while parts and parts[0].lower() in _HONORIFICS:
        parts = parts[1:]
    return " ".join(parts)


def _name_key(name: str) -> str:
    return re.sub(r"\s+", "", _strip_honorifics(name)).casefold()


def build_canonical_map(
    blocks: list[dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, str]:
    from collections import Counter
    freq: Counter = Counter()
    for b in blocks:
        for name, _cat, _frak in b["members"]:
            freq[aliases.get(name, name)] += 1
    groups: dict[str, list[str]] = defaultdict(list)
    for name in freq:
        groups[_name_key(name)].append(name)
    canon_of_target: dict[str, str] = {}
    for _key, names in groups.items():
        best = max(
            names,
            key=lambda n: (freq[n], _strip_honorifics(n) == n, -len(n), n),
        )
        for n in names:
            canon_of_target[n] = best
    result: dict[str, str] = {}
    raw_names = {name for b in blocks for name, _c, _f in b["members"]}
    for raw in raw_names:
        target = aliases.get(raw, raw)
        result[raw] = canon_of_target.get(target, target)
    return result


def build_kadencja(
    blocks: list[dict[str, Any]],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any]:
    kadencje = config.get("kadencje", {})
    kdef = kadencje.get(kadencja_id) or {}
    start_date = kdef.get("start", "")
    result_map = config.get("result_text_map", {})

    kad_blocks = [
        b for b in blocks
        if b.get("session_date") and (not start_date or b["session_date"] >= start_date)
    ]

    canon = build_canonical_map(kad_blocks, config.get("name_aliases", {}))

    all_names: set[str] = set()
    for b in kad_blocks:
        for name, _cat, _frak in b["members"]:
            all_names.add(canon.get(name, name))
    councilor_index = sorted(all_names)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    club_by_name: dict[str, str] = {}
    for b in sorted(kad_blocks, key=lambda x: x.get("session_date", "")):
        for name, _cat, frak in b["members"]:
            if frak:
                cname = canon.get(name, name)
                club_by_name[cname] = frak

    votes_flat: list[dict[str, Any]] = []
    sessions_meta: dict[str, dict[str, Any]] = {}

    for b in kad_blocks:
        date = b["session_date"]
        counts = {c: 0 for c in CATEGORIES}
        named_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}
        seen_idx: set[int] = set()
        for name, cat, _frak in b["members"]:
            idx = name_to_idx.get(canon.get(name, name))
            if idx is None or idx in seen_idx:
                continue
            seen_idx.add(idx)
            counts[cat] += 1
            named_idx[cat].append(idx)

        szama_sanit = re.sub(r"[^0-9A-Za-z]+", "_", b.get("szama", "")).strip("_")
        vote_id = f"szeged_{szama_sanit}" if szama_sanit else f"szeged_{date}_{len(votes_flat)}"

        votes_flat.append({
            "id": vote_id,
            "session_date": date,
            "session_number": None,
            "source_url": b.get("source_url", ""),
            "topic": b.get("topic", ""),
            "druk": b.get("szama", ""),
            "resolution": "",
            "result": normalize_result(b.get("result_native", ""), result_map),
            "result_native": b.get("result_native", ""),
            "counts": counts,
            "named_votes": named_idx,
            "voted_at": b.get("voted_at", ""),
        })

        sess = sessions_meta.setdefault(date, {
            "date": date,
            "vote_ids": [],
            "attendees": set(),
            "source_url": b.get("source_url", ""),
        })
        sess["vote_ids"].append(vote_id)
        for name, cat, _frak in b["members"]:
            if cat != "nieobecni":
                sess["attendees"].add(canon.get(name, name))

    sessions: list[dict[str, Any]] = []
    for date, sess in sessions_meta.items():
        attendees_list = sorted(sess["attendees"])
        sessions.append({
            "date": date,
            "number": None,
            "title": f"Szeged Közgyűlés {date}",
            "start": "",
            "end": "",
            "vote_count": len(sess["vote_ids"]),
            "attendee_count": len(attendees_list),
            "attendees": attendees_list,
            "source_url": sess["source_url"],
        })
    sessions.sort(key=lambda s: s["date"])

    return {
        "sessions": sessions,
        "votes": votes_flat,
        "councilor_index": councilor_index,
        "club_by_name": club_by_name,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id")
    parser.add_argument("--max-sessions", type=int, default=0)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--pdf", type=Path,
                        help="Tryb offline: parsuj lokalny PDF i wypisz bloki (debug).")
    parser.add_argument("--pdf-text", type=Path,
                        help="Tryb offline: parsuj lokalny wyciąg tekstowy (debug).")
    args = parser.parse_args()

    # --- Tryby offline ---
    if args.pdf or args.pdf_text:
        if args.pdf_text:
            text = Path(args.pdf_text).read_text(encoding="utf-8")
        else:
            text = pdf_to_text(args.pdf)
        blocks = parse_jegyzokonyv_text(text)
        print(f"[szeged] bloków głosowań: {len(blocks)}", file=sys.stderr)
        for b in blocks:
            cats = defaultdict(int)
            for _n, c, _f in b["members"]:
                cats[c] += 1
            print(json.dumps({
                "szama": b["szama"], "voted_at": b["voted_at"],
                "tipus": b["tipus"], "result_native": b["result_native"],
                "topic": b["topic"][:80], "members": len(b["members"]),
                "counts": dict(cats),
            }, ensure_ascii=False))
        return 0

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    cache = None if args.skip_fetch else args.cache
    api_base = config.get("timportal_api", "https://eservices.szeged.eu/tim_public")
    docs = args.docs
    docs.mkdir(parents=True, exist_ok=True)

    # Pobierz listę sesji z API
    print(f"[szeged] GET lista sesji z API", file=sys.stderr)
    all_sessions: list[dict[str, Any]] = []
    # Sprawdź ostatnie 3 lata (2024-2026 dla obecnej kadencji)
    current_year = datetime.now().year
    for year in range(current_year, current_year - 3, -1):
        sessions = get_sessions_from_api(api_base, year, cache)
        all_sessions.extend(sessions)
        print(f"[szeged] {year}: {len(sessions)} sesji", file=sys.stderr)

    all_sessions.sort(key=lambda s: s["date"])
    print(f"[szeged] łącznie {len(all_sessions)} sesji", file=sys.stderr)

    if args.max_sessions:
        all_sessions = all_sessions[: args.max_sessions]
        print(f"[szeged] LIMIT: {len(all_sessions)} sesji", file=sys.stderr)

    pdf_cache = args.cache / "pdfs"
    text_cache = args.cache / "text"
    text_cache.mkdir(parents=True, exist_ok=True)

    all_blocks: list[dict[str, Any]] = []
    for i, sess in enumerate(all_sessions, 1):
        date = sess["date"]
        guid = sess["guid"]
        ules_id = sess["ules_id"]
        # URL do pobrania PDF
        url = f"{api_base}/download_public_file.php?guid={guid}&ules={ules_id}&t=h"
        print(f"[szeged] [{i}/{len(all_sessions)}] {date}", file=sys.stderr)

        pdf_path = pdf_cache / f"{_cache_key(url)}.pdf"
        text_path = text_cache / f"{_cache_key(url)}.txt"
        if text_path.is_file():
            text = text_path.read_text(encoding="utf-8")
        else:
            if not http_download(url, pdf_path):
                print(f"  PDF download FAILED: {url}", file=sys.stderr)
                continue
            text = pdf_to_text(pdf_path)
            text_path.write_text(text, encoding="utf-8")
        blocks = parse_jegyzokonyv_text(text)
        for b in blocks:
            b["source_url"] = url
            if not b.get("session_date"):
                b["session_date"] = date
        print(f"  {len(blocks)} głosowań imiennych", file=sys.stderr)
        all_blocks.extend(blocks)

    print(f"[szeged] łącznie {len(all_blocks)} głosowań imiennych", file=sys.stderr)

    # Sprzątanie starych kadencji
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            old.unlink()

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kadencje_to_generate = (
        [args.kadencja_id] if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    all_clubs: dict[str, str] = {}
    for kid in kadencje_to_generate:
        kdef = config["kadencje"][kid]
        built = build_kadencja(all_blocks, config, kid)
        if not built["votes"]:
            print(f"[szeged] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue
        out = {
            "id": kid,
            "label": kdef.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": built["sessions"],
            "votes": built["votes"],
            "councilor_index": built["councilor_index"],
        }
        out_path = docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[szeged] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )
        all_clubs.update(built["club_by_name"])

    club_assignments_path = docs / "club_assignments.json"
    with open(club_assignments_path, "w", encoding="utf-8") as f:
        json.dump(all_clubs, f, ensure_ascii=False, indent=2)
    print(f"[szeged] wrote club_assignments.json: {len(all_clubs)} radnych",
          file=sys.stderr)

    profiles = {name: {"name": name, "club": club} for name, club in all_clubs.items()}
    profiles_path = docs / "profiles.json"
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": scraped_at, "profiles": profiles},
                  f, ensure_ascii=False, indent=2)
    print(f"[szeged] wrote profiles.json: {len(profiles)} radnych", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
