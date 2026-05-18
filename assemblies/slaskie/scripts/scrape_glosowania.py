#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Śląskiego, VII kadencja 2024-2029.

Źródło: bip.slaskie.pl/sejmik_wojewodztwa/sesje_sejmiku/glosowania_radnych/

Pipeline:
1. Crawl listing kadencji per rok: /glosowania_radnych/?p=YYYY
2. Per rok, lista miesięcy linki w body
3. Per miesiąc, lista sesji (linki do podstron sesji)
4. Per sesja: pobierz ?format=json → attachments[].src → URL do PDF
5. PDF zawiera N stron, każda strona = jedno głosowanie wygenerowane przez
   app.esesja.pl z formatem:

        Głosowano w sprawie: <topic>
        ZA: N, PRZECIW: N, WSTRZYMUJĘ SIĘ: N, BRAK GŁOSU: N, NIEOBECNI: N
        Wyniki imienne:
        ZA (N)
        <lista imion oddzielonych przecinkami>
        PRZECIW (N)
        ...
        Głosowanie z dnia: DD.MM.YYYY, HH:MM:SS

Output: kadencja-2024-2029.json (schema mazowieckiego/dolnośląskiego).

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-sessions 1
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE = "https://bip.slaskie.pl"
GLOSOWANIA_ROOT = f"{BASE}/sejmik_wojewodztwa/sesje_sejmiku/glosowania_radnych/"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
KADENCJA_START_DATE = "2024-05-07"
USER_AGENT = "Mozilla/5.0 Radoskop/1.0"
TIMEOUT = 30
SLEEP = 0.1

PL_MONTHS = {
    "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10, "listopad": 11, "grudzień": 12,
}


def _ck(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def fetch_bytes(url: str, cache_dir: Path | None) -> bytes:
    cache = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"{_ck(url)}.bin"
        if cache.is_file():
            return cache.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    if cache:
        cache.write_bytes(data)
    time.sleep(SLEEP)
    return data


def fetch_text(url: str, cache_dir: Path | None) -> str:
    return fetch_bytes(url, cache_dir).decode("utf-8", "replace")


def fetch_json(url: str, cache_dir: Path | None) -> dict:
    return json.loads(fetch_text(url, cache_dir))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def list_session_urls(cache_dir: Path | None) -> list[str]:
    """Crawl /glosowania_radnych/?p=YYYY → /?p=YYYY^month → URL-e sesji.

    Filtruje na kadencja 2024-2029 (sesje od 2024-05-07).
    """
    out: list[str] = []
    for year in [2024, 2025, 2026]:
        year_url = f"{GLOSOWANIA_ROOT}?p={year}"
        text = fetch_text(year_url, cache_dir)
        # Linki do miesięcy z pattern ?p=YYYY%5E<month>
        # Uwaga: hrefy są często względne (np. "?p=2024%5Emaj") — trzeba je
        # rozwiązać względem year_url (z prefiksem .../glosowania_radnych/),
        # nie BASE, bo inaczej wpadamy na stronę główną BIP bez listy sesji.
        months = re.findall(r'href="([^"]*\?p=' + str(year) + r'%5E[^"]+)"', text)
        for month_href in set(months):
            month_url = urljoin(year_url, month_href)
            month_text = fetch_text(month_url, cache_dir)
            # Linki do sesji
            session_paths = re.findall(
                r'href="([^"]+/glosowania_radnych/sesja-sejmiku-[^"]+\.html)"',
                month_text,
            )
            for path in set(session_paths):
                full = urljoin(BASE, path)
                if full not in out:
                    out.append(full)
    return out


def fetch_session_attachments(session_url: str, cache_dir: Path | None) -> tuple[str, str]:
    """Pobierz JSON sesji i wyciągnij URL do PDF z imiennymi głosowaniami.

    Zwraca (pdf_url, session_title).
    """
    json_url = session_url + "?format=json"
    data = fetch_json(json_url, cache_dir)
    title = data.get("title") or ""
    components = data.get("components") or []
    for c in components:
        if c.get("type") == "Attachment":
            content = c.get("content")
            atts = content if isinstance(content, list) else (
                content.get("files") or content.get("attachments") or []
                if isinstance(content, dict) else []
            )
            for a in atts:
                if isinstance(a, dict) and a.get("extension", "").upper() == "PDF":
                    src = a.get("src", "")
                    if src:
                        full = src if src.startswith("http") else f"{BASE}{src}"
                        return full, title
    return "", title


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

VOTE_CATEGORIES = ["ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ", "BRAK GŁOSU", "NIEOBECNI"]
CAT_TO_KEY = {
    "ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu", "NIEOBECNI": "nieobecni",
}


def parse_voting_page(text: str, source_url: str) -> dict | None:
    """Parsuj jedną stronę PDF z app.esesja.pl format."""
    # Normalize whitespace
    plain = re.sub(r"\s+", " ", text).strip()
    if "Głosowano w sprawie" not in plain:
        return None

    # Topic
    topic = ""
    m = re.search(r"Głosowano w sprawie:\s*(.+?)\s+ZA:\s*\d+,", plain)
    if m:
        topic = m.group(1).strip()

    # Counts
    counts = {k: 0 for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
    m = re.search(
        r"ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ\s+SIĘ:\s*(\d+),\s*BRAK\s+GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)",
        plain,
    )
    if m:
        counts["za"] = int(m.group(1))
        counts["przeciw"] = int(m.group(2))
        counts["wstrzymal_sie"] = int(m.group(3))
        counts["brak_glosu"] = int(m.group(4))
        counts["nieobecni"] = int(m.group(5))

    # Data głosowania
    voted_at = ""
    session_date = ""
    m = re.search(r"Głosowanie z dnia:\s*(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{1,2}):(\d{2}):(\d{2})", plain)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        h, mi, s = m.group(4), m.group(5), m.group(6)
        session_date = f"{y}-{mo}-{d}"
        voted_at = f"{session_date}T{int(h):02d}:{mi}:{s}"

    # Imienne listy
    names_by_cat: dict[str, list[str]] = {k: [] for k in counts}
    after = plain.split("Wyniki imienne:", 1)
    if len(after) == 2:
        details = after[1].split("Głosowanie z dnia:")[0]
        # Każda kategoria: "ZA (N) <names>" / "PRZECIW (N) <names>" ...
        # Use regex with lookahead na kolejną kategorię
        cat_pattern = "|".join(re.escape(c) for c in VOTE_CATEGORIES)
        for m in re.finditer(
            rf"({cat_pattern})\s*\(\d+\)\s*(.*?)(?=(?:{cat_pattern})\s*\(|$)",
            details,
        ):
            label = m.group(1).upper()
            chunk = m.group(2).strip()
            key = CAT_TO_KEY.get(label)
            if not key or not chunk:
                continue
            # Imiona oddzielone przecinkami
            names = [n.strip() for n in chunk.split(",") if n.strip()]
            # Filtr: muszą wyglądać jak imię + nazwisko
            valid_names = []
            for n in names:
                # Min 2 słowa, każde TitleCase, brak liczb/dziwnych znaków
                parts = n.split()
                if len(parts) >= 2 and all(p[0].isupper() for p in parts if p):
                    valid_names.append(n)
            names_by_cat[key] = valid_names

    return {
        "session_date": session_date,
        "voted_at": voted_at,
        "topic": topic,
        "counts": counts,
        "named_votes": names_by_cat,
        "source_url": source_url,
    }


def parse_pdf(pdf_bytes: bytes, source_base: str) -> list[dict]:
    """Parsuj wszystkie strony PDF — każda strona to osobne głosowanie."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print("UWAGA: zainstaluj pypdf", file=sys.stderr)
        raise
    reader = PdfReader(io.BytesIO(pdf_bytes))
    votes = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        v = parse_voting_page(text, source_url=f"{source_base}#page={i}")
        if v and v["session_date"]:
            votes.append(v)
    return votes


# ---------------------------------------------------------------------------
# Orchestracja
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            for n in names:
                seen.add(n)
    sorted_names = sorted(seen)
    return sorted_names, {n: i for i, n in enumerate(sorted_names)}


def votes_to_index(vote: dict, idx: dict[str, int], seq: int) -> dict:
    return {
        "id": f"{vote['session_date']}_{seq}",
        "session_date": vote["session_date"],
        "session_number": "",
        "source_url": vote["source_url"],
        "topic": vote["topic"],
        "druk": None,
        "resolution": None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(idx[n] for n in names if n in idx)
            for cat, names in vote["named_votes"].items()
        },
        "voted_at": vote["voted_at"],
    }


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        if not d:
            continue
        sess = by_date.setdefault(d, {
            "date": d, "number": "", "vote_count": 0,
            "attendees": set(), "attendee_count": 0, "speakers": [],
        })
        sess["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for n in v["named_votes"].get(cat, []):
                sess["attendees"].add(n)
    out = []
    for d in sorted(by_date.keys(), reverse=True):
        s = by_date[d]
        s["attendees"] = sorted(s["attendees"])
        s["attendee_count"] = len(s["attendees"])
        out.append(s)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cache = Path(args.cache_dir) if args.cache_dir else None

    print(f"=== Radoskop Sejmik Śląski (BIP {BASE}) ===\n")
    print("[1/4] Crawl listy sesji...")
    session_urls = list_session_urls(cache)
    print(f"  Znaleziono {len(session_urls)} sesji")
    if args.max_sessions:
        session_urls = session_urls[: args.max_sessions]

    print(f"\n[2/4] Pobieranie PDF-ów {len(session_urls)} sesji...")
    all_votes = []
    for i, url in enumerate(session_urls, 1):
        try:
            pdf_url, title = fetch_session_attachments(url, cache)
        except Exception as exc:
            print(f"  [{i}/{len(session_urls)}] {url}: ERR {exc}")
            continue
        if not pdf_url:
            print(f"  [{i}/{len(session_urls)}] {title[:60]}: brak PDF, skip")
            continue
        print(f"  [{i}/{len(session_urls)}] {title[:60]}")
        if args.dry_run:
            continue
        try:
            pdf_bytes = fetch_bytes(pdf_url, cache)
            votes = parse_pdf(pdf_bytes, pdf_url)
            # Filtr na kadencję VII (od 2024-05-07). BIP zwraca też sesje
            # poprzedniej kadencji jeśli mieszczą się w przeglądanych latach.
            before = len(votes)
            votes = [v for v in votes if v.get("session_date", "") >= KADENCJA_START_DATE]
            skipped = before - len(votes)
            all_votes.extend(votes)
            msg = f"    {len(votes)} głosowań"
            if skipped:
                msg += f" (pominięto {skipped} z poprzedniej kadencji)"
            print(msg)
        except Exception as exc:
            print(f"    PDF fetch/parse: {exc}")

    if args.dry_run:
        return 0

    print(f"\n[3/4] Build councilor_index z {len(all_votes)} głosowań...")
    councilors, name_to_idx = build_councilor_index(all_votes)
    print(f"  Radnych unikalnych: {len(councilors)}")

    indexed = [votes_to_index(v, name_to_idx, i) for i, v in enumerate(all_votes)]
    out_sessions = aggregate_sessions(all_votes)

    print(f"\n[4/4] Zapisuję {args.output}...")
    output = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "sessions": out_sessions,
        "total_sessions": len(out_sessions),
        "total_votes": len(indexed),
        "total_councilors": len(councilors),
        "councilors": [], "votes": indexed,
        "similarity_top": [], "similarity_bottom": [],
        "councilor_index": councilors,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"  {len(out_sessions)} sesji, {len(indexed)} głosowań, {len(councilors)} radnych")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
