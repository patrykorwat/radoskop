#!/usr/bin/env python3
"""
Scraper Szolnok Megyei Jogú Város Közgyűlésének szavazásai.

ŹRÓDŁO DANYCH I DLACZEGO DZIAŁA
================================
Szolnok publikuje protokoły (jegyzőkönyv) posiedzeń rady jako PDF na stronie
szolnok.hu/kozgyulesi-jegyzokonyvek/. System używa WordPress + Download
Monitor, a PDF-y są chronione przed bezpośrednim pobieraniem (security
plugin). Do pobierania PDF-ów wymagany jest Playwright (prawdziwa
przeglądarka).

Struktura danych:
- Nowa strona (2026+): React app z Download Monitor, dane ładowane dynamicznie
- Archiwum (2007-2025): statyczne HTML z linkami do PDF

PDF-y zawierają protokoły z głosowaniami w formacie "Szavazás eredménye"
z imienną tabelą Név/Voks/Frakció (identycznie jak Budapeszt i Szeged).

PIPELINE
========
1. Pobierz listę lat z archiwum (info_szolnok_hu_archivum/...)
2. Per rok: pobierz listę PDF-ów
3. Pobierz każdy PDF przez Playwright (security plugin blokuje curl)
4. pdftotext -> tekst, split na bloki "Szavazás eredménye"
5. Per blok wyciągnij imienną tabelę Név/Voks/Frakció
6. Zbuduj docs/kadencja-{id}.json

Wymaga:
- playwright (pip install playwright)
- chromium (python3 -m playwright install chromium)
- pdftotext (poppler-utils)

Użycie:
    python3 scrape_szolnok.py
    python3 scrape_szolnok.py --max-sessions 2
    python3 scrape_szolnok.py --skip-fetch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "[szolnok] BLAD: brak playwright. Instalacja:\n"
        "  pip install playwright --break-system-packages\n"
        "  python3 -m playwright install chromium",
        file=sys.stderr,
    )
    raise


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 90
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.5

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

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

ARCHIVE_BASE = "https://www.szolnok.hu/info_szolnok_hu_archivum/kozgyulesi-jegyzokonyvek-m306"


def _cache_key(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def have_command(name: str) -> bool:
    return shutil.which(name) is not None


def pdf_to_text(pdf_path: Path) -> str:
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
# Parser bloku "Szavazás eredménye" (identyczny jak Budapeszt/Szeged)
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
# Pobieranie listy PDF-ów z archiwum
# ---------------------------------------------------------------------------

def http_get_text(url: str, cache_dir: Path | None) -> str:
    cache_file: Path | None = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.html"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
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


def get_archive_years(cache: Path | None) -> list[tuple[int, str]]:
    """Pobiera listę lat i URL-i dostępnych w archiwum.

    Zwraca listę (year, url) posortowaną malejąco po roku.
    """
    url = f"{ARCHIVE_BASE}/kozgyulesi-jegyzokonyvek-m306.html"
    html = http_get_text(url, cache)
    years: list[tuple[int, str]] = []
    for m in re.finditer(
        r'href="(kozgyulesi-jegyzokonyvek-(\d{4})-n\d+\.html)"',
        html,
    ):
        year_url = m.group(1)
        year = int(m.group(2))
        if year not in [y for y, _ in years]:
            years.append((year, f"{ARCHIVE_BASE}/{year_url}"))
    return sorted(years, key=lambda x: x[0], reverse=True)


def get_archive_pdfs(year_url: str, cache: Path | None) -> list[dict[str, str]]:
    """Pobiera listę PDF-ów dla danego roku z archiwum."""
    html = http_get_text(year_url, cache)

    pdfs: list[dict[str, str]] = []
    for m in re.finditer(
        r'href="(/info_szolnok_hu_archivum/files/[^"]+\.pdf)"',
        html,
    ):
        pdf_url = urljoin("https://www.szolnok.hu", m.group(1))
        pdfs.append({"url": pdf_url})
    return pdfs


# ---------------------------------------------------------------------------
# Pobieranie PDF przez Playwright (security plugin blokuje curl)
# ---------------------------------------------------------------------------

def download_pdf_playwright(url: str, target: Path) -> bool:
    """Pobiera PDF przez Playwright (prawdziwa przeglądarka)."""
    if target.is_file() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                accept_downloads=True,
            )
            page = context.new_page()
            # Navigate to PDF - it will either display inline or trigger download
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Try to get the PDF content
            content = page.content()
            if "The specified URL cannot be found" in content:
                print(f"  BLOCKED: {url}", file=sys.stderr)
                browser.close()
                return False

            # Save page as PDF or get the binary content
            # Some sites serve PDF inline, others as download
            # Try response body
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response:
                body = response.body()
                if body[:4] == b"%PDF":
                    target.write_bytes(body)
                    browser.close()
                    return True

            # If that didn't work, try using CDP to get the PDF
            # Some servers block headless browsers - try with a real-looking UA
            browser.close()
            return False
    except Exception as exc:
        print(f"  Playwright error: {exc}", file=sys.stderr)
        return False


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
        vote_id = f"szolnok_{szama_sanit}" if szama_sanit else f"szolnok_{date}_{len(votes_flat)}"

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
            "title": f"Szolnok Közgyűlés {date}",
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

    if args.pdf or args.pdf_text:
        if args.pdf_text:
            text = Path(args.pdf_text).read_text(encoding="utf-8")
        else:
            text = pdf_to_text(args.pdf)
        blocks = parse_jegyzokonyv_text(text)
        print(f"[szolnok] bloków głosowań: {len(blocks)}", file=sys.stderr)
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
    docs = args.docs
    docs.mkdir(parents=True, exist_ok=True)

    # Pobierz listę lat z archiwum
    print(f"[szolnok] GET lista lat z archiwum", file=sys.stderr)
    year_urls = get_archive_years(cache)
    print(f"[szolnok] lata: {[y for y, _ in year_urls]}", file=sys.stderr)

    # Pobierz listę PDF-ów
    all_pdfs: list[dict[str, str]] = []
    for year, year_url in year_urls:
        pdfs = get_archive_pdfs(year_url, cache)
        all_pdfs.extend(pdfs)
        print(f"[szolnok] {year}: {len(pdfs)} PDF-ów", file=sys.stderr)

    print(f"[szolnok] łącznie {len(all_pdfs)} PDF-ów", file=sys.stderr)

    if args.max_sessions:
        all_pdfs = all_pdfs[: args.max_sessions]
        print(f"[szolnok] LIMIT: {len(all_pdfs)} PDF-ów", file=sys.stderr)

    pdf_cache = args.cache / "pdfs"
    text_cache = args.cache / "text"
    text_cache.mkdir(parents=True, exist_ok=True)

    all_blocks: list[dict[str, Any]] = []
    for i, pdf_info in enumerate(all_pdfs, 1):
        url = pdf_info["url"]
        print(f"[szolnok] [{i}/{len(all_pdfs)}] {url}", file=sys.stderr)

        pdf_path = pdf_cache / f"{_cache_key(url)}.pdf"
        text_path = text_cache / f"{_cache_key(url)}.txt"

        if text_path.is_file():
            text = text_path.read_text(encoding="utf-8")
        else:
            if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                ok = download_pdf_playwright(url, pdf_path)
                if not ok:
                    print(f"  PDF download FAILED (blocked)", file=sys.stderr)
                    continue
            text = pdf_to_text(pdf_path)
            if not text.strip():
                print(f"  PDF empty or scanned", file=sys.stderr)
                continue
            text_path.write_text(text, encoding="utf-8")

        blocks = parse_jegyzokonyv_text(text)
        for b in blocks:
            b["source_url"] = url
        print(f"  {len(blocks)} głosowań imiennych", file=sys.stderr)
        all_blocks.extend(blocks)

    print(f"[szolnok] łącznie {len(all_blocks)} głosowań imiennych", file=sys.stderr)

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
            print(f"[szolnok] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
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
            f"[szolnok] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )
        all_clubs.update(built["club_by_name"])

    club_assignments_path = docs / "club_assignments.json"
    with open(club_assignments_path, "w", encoding="utf-8") as f:
        json.dump(all_clubs, f, ensure_ascii=False, indent=2)
    print(f"[szolnok] wrote club_assignments.json: {len(all_clubs)} radnych",
          file=sys.stderr)

    profiles = {name: {"name": name, "club": club} for name, club in all_clubs.items()}
    profiles_path = docs / "profiles.json"
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": scraped_at, "profiles": profiles},
                  f, ensure_ascii=False, indent=2)
    print(f"[szolnok] wrote profiles.json: {len(profiles)} radnych", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
