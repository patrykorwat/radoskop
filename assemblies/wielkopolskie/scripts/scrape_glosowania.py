#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Wielkopolskiego, VII kadencja 2024-2029.

Źródło: bip.umww.pl — statyczny HTML BIP urzędu marszałkowskiego.

Architektura: każde głosowanie ma osobną podstronę HTML z imienną listą
głosujących w formacie:

    Data i godzina głosowania: 2026-04-27 13:36:00
    Informacja o głosowaniu:
    Liczba uprawnionych 39
    Liczba obecnych 35
    Liczba nieobecnych 4
    Liczba oddanych głosów 25
    Głosy za 16
    Głosy przeciw 0
    Głosy wstrzymujące się 9
    Osoby obecne niegłosujące 10
    Lista głosujących:
    Uczestnik Głosowanie
    Bierła Leszek WSTRZYMUJĘ SIĘ
    Bogrycewicz Adam WSTRZYMUJĘ SIĘ
    Bogusławski Jacek OBECNY
    ...

URL pattern:
    https://bip.umww.pl/imienne-wykazy-glosowan---{vote}---{seq}---{cat}---{session}
    np. {vote=24, seq=3, cat=7, session=93} = sesja XXIV, 3. głosowanie

Pipeline:
1. Crawl root listing /106---imienne-wykazy-glosowan → linki do sesji
2. Per sesja /imienne-wykazy-glosowan---{N} → linki do głosowań
3. Per głosowanie → parse plain text → struktura schema-zgodna z mazowieckim

Schemat wyjścia: zgodny z mazowieckim/dolnośląskim (id, label, sessions,
votes, councilor_index, total_*).

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-sessions 1
    python3 scrape_glosowania.py --output /tmp/wlkp.json --cache-dir /tmp/wlkp_cache
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE = "https://bip.umww.pl"
ROOT_LISTING = f"{BASE}/106---imienne-wykazy-glosowan"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
USER_AGENT = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Radoskop/1.0 (+https://radoskop.pl)"
TIMEOUT = 30
SLEEP = 0.1


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def fetch_text(url: str, *, cache_dir: Path | None = None) -> str:
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.html"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT) as r:
        data = r.read().decode("utf-8", "replace")
    if cache_file:
        cache_file.write_text(data, encoding="utf-8")
    time.sleep(SLEEP)
    return data


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_sessions(cache_dir: Path | None) -> list[dict]:
    """Z root listing zbierz linki do per-sesja stron.

    URL pattern: imienne-wykazy-glosowan---{N} (N = unikalny ID sesji,
    np. 93 dla XXIV). Tekst linku zawiera numer rzymski sesji.
    Filtruje VI kadencja (mają osobny rodzic).
    """
    text = fetch_text(ROOT_LISTING, cache_dir=cache_dir)
    sessions = []
    seen = set()
    # Match: <a href="imienne-wykazy-glosowan---93">XXIV Sesja Sejmiku...</a>
    for m in re.finditer(
        r'<a[^>]*href="(imienne-wykazy-glosowan---\d+)"[^>]*>([^<]+)</a>',
        text,
    ):
        url_part, title = m.group(1), m.group(2).strip()
        if url_part in seen:
            continue
        seen.add(url_part)
        # Wyciągnij rzymski numer sesji z tytułu
        roman_match = re.match(r"([IVXLCDM]+)\s+(?:Nadzwyczajna\s+)?Sesja", title, re.I)
        if not roman_match:
            continue
        roman = roman_match.group(1).upper()
        sessions.append({
            "url": f"{BASE}/{url_part}",
            "title": title,
            "roman": roman,
            "menu_id": int(url_part.rsplit("---", 1)[1]),
        })
    return sessions


def discover_votes_in_session(session_url: str, cache_dir: Path | None) -> list[dict]:
    """Z per-sesja podstrony zbierz linki do każdego głosowania.

    URL pattern: imienne-wykazy-glosowan---{vote}---{seq}---{cat}---{session}
    np. -24---3---7---93 = sesja XXIV vote 3
    """
    text = fetch_text(session_url, cache_dir=cache_dir)
    votes = []
    for m in re.finditer(
        r'<a[^>]*href="(imienne-wykazy-glosowan---\d+---\d+---\d+---\d+)"[^>]*>([^<]+)</a>',
        text,
    ):
        url_part, title = m.group(1), m.group(2).strip()
        votes.append({
            "url": f"{BASE}/{url_part}",
            "topic": html_lib.unescape(title),
        })
    return votes


# ---------------------------------------------------------------------------
# Parse pojedynczego głosowania
# ---------------------------------------------------------------------------

VOTE_LABEL_MAP = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie",  # diakrytyki vs zwykłe E
    "OBECNY": "brak_glosu",
    "OBECNA": "brak_glosu",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
}


def _names_lookup() -> dict[str, str]:
    """Mapa "Nazwisko Imię" → "Imię Nazwisko" z config.json (PKW source).

    Wielkopolski BIP wyświetla nazwiska w kolejności "Nazwisko Imię"
    (np. "Bierła Leszek"), config trzyma "Imię Nazwisko" ("Leszek Bierła").
    Mapowanie eliminuje konieczność zgadywania który token to imię.
    """
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.is_file():
        return {}
    try:
        names = json.loads(cfg.read_text(encoding="utf-8")).get("club_assignments", {})
    except Exception:
        return {}
    out = {}
    for full in names:
        parts = full.split()
        if len(parts) >= 2:
            # last word = surname, first word = first name
            surname = parts[-1]
            first = parts[0]
            out[f"{surname} {first}"] = full
    return out


def parse_voting_page(html: str, source_url: str) -> dict | None:
    """Parsuj jedno głosowanie z HTML strony.

    Strategia: usuwamy tagi HTML, odzyskujemy plain text, parsujemy słowa-anchory.
    Wzorce:
      "Data i godzina głosowania: 2026-04-27 13:36:00"
      "Liczba uprawnionych N", "Głosy za N", itd.
      Po "Lista głosujących: Uczestnik Głosowanie" lista nazwisk z głosami.
    """
    plain = re.sub(r'<[^>]+>', ' ', html)
    plain = html_lib.unescape(plain)
    plain = re.sub(r'\s+', ' ', plain)

    # Tytuł — szukamy w treści tytułu sesji + uchwały, lecz parse_voting_page
    # dostaje tylko stronę głosowania. Tytuł jest w nagłówku artykułu
    # (po "<h1>" lub w meta). Bezpieczniej zostawić topic do callera (z
    # discover_votes_in_session).

    # Data i godzina
    voted_at = ""
    session_date = ""
    m = re.search(r"Data i godzina głosowania:\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}:\d{2})", plain)
    if m:
        session_date = m.group(1)
        voted_at = f"{m.group(1)}T{m.group(2)}"

    # Liczby zbiorcze
    def _num(label: str) -> int:
        m = re.search(rf"{label}\s+(\d+)", plain)
        return int(m.group(1)) if m else 0

    counts = {
        "za": _num(r"Głosy za"),
        "przeciw": _num(r"Głosy przeciw"),
        "wstrzymal_sie": _num(r"Głosy wstrzymujące się"),
        "brak_glosu": _num(r"Osoby obecne niegłosujące"),
        "nieobecni": _num(r"Liczba nieobecnych"),
    }

    # Lista głosujących
    names_by_cat: dict[str, list[str]] = {k: [] for k in counts}
    after = plain.split("Lista głosujących:", 1)
    if len(after) == 2:
        details = after[1]
        # Stop na "wytworzenie informacji" (metryka strony) albo "Rejestr zmian"
        for stop in ["wytworzenie informacji", "Rejestr zmian", "Urząd Marszałkowski"]:
            if stop in details:
                details = details.split(stop)[0]
                break
        details = details.replace("Uczestnik Głosowanie", "", 1)
        lookup = _names_lookup()
        # Każde wystąpienie "Nazwisko Imię GŁOS" — pattern: 2-3 tokeny TitleCase
        # potem słowo-anchor (ZA/PRZECIW/WSTRZYMUJĘ SIĘ/OBECNY/NIEOBECNY).
        # Lista głosów-anchor: trzymamy w order maleńcym żeby najpierw match
        # 2-słowowe "WSTRZYMUJĘ SIĘ" zamiast "WSTRZYMUJĘ" + "SIĘ".
        labels_re = r"(WSTRZYMUJĘ\s+SIĘ|WSTRZYMUJE\s+SIĘ|NIEOBECNY|NIEOBECNA|OBECNY|OBECNA|PRZECIW|ZA)"
        # Iteruj po każdym match — wycinek przed labelem zawiera nazwisko
        last_end = 0
        for m in re.finditer(labels_re, details):
            label = re.sub(r"\s+", " ", m.group(0).upper())
            cat = VOTE_LABEL_MAP.get(label)
            if not cat:
                continue
            # Nazwisko = tokens between last_end and m.start()
            chunk = details[last_end : m.start()].strip()
            last_end = m.end()
            tokens = chunk.split()
            if len(tokens) < 2:
                continue
            # "Nazwisko Imię" — bierz ostatnie 2 tokeny (część przed mogła
            # być end poprzedniego nazwiska + nazwa kolejnego)
            # ale uwzględnij myślniki w nazwisku (Rzepecka-Andrzejak Katarzyna)
            # Bierz ostatnie 2 tokeny jako (nazwisko, imię).
            surname = tokens[-2]
            first = tokens[-1]
            key = f"{surname} {first}"
            canonical = lookup.get(key, f"{first} {surname}")
            names_by_cat[cat].append(canonical)

    return {
        "session_date": session_date,
        "voted_at": voted_at,
        "counts": counts,
        "named_votes": names_by_cat,
        "source_url": source_url,
    }


# ---------------------------------------------------------------------------
# Orchestracja
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            for name in names:
                seen.add(name)
    sorted_names = sorted(seen)
    return sorted_names, {n: i for i, n in enumerate(sorted_names)}


def votes_to_index(vote: dict, topic: str, roman: str, name_to_idx: dict[str, int]) -> dict:
    vote_id = f"{vote['session_date']}_{vote['voted_at'][-8:].replace(':','')}"
    return {
        "id": vote_id,
        "session_date": vote["session_date"],
        "session_number": roman,
        "source_url": vote["source_url"],
        "topic": topic,
        "druk": None,
        "resolution": None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(name_to_idx[n] for n in names if n in name_to_idx)
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
            "date": d, "number": v.get("session_number", ""),
            "vote_count": 0, "attendees": set(), "attendee_count": 0, "speakers": [],
        })
        sess["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for name in v["named_votes"].get(cat, []):
                sess["attendees"].add(name)
    out = []
    for d in sorted(by_date.keys(), reverse=True):
        s = by_date[d]
        s["attendees"] = sorted(s["attendees"])
        s["attendee_count"] = len(s["attendees"])
        out.append(s)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Scraper Sejmiku Wielkopolskiego (BIP UMWW HTML)")
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print(f"=== Radoskop Sejmik Wielkopolski (BIP {BASE}) ===\n")
    print("[1/3] Discover sessions...")
    sessions = discover_sessions(cache_dir)
    print(f"  Znaleziono {len(sessions)} sesji VII kadencji")
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    print(f"\n[2/3] Pobieranie głosowań z {len(sessions)} sesji...")
    all_votes: list[dict] = []
    for i, sess in enumerate(sessions, 1):
        try:
            votes_meta = discover_votes_in_session(sess["url"], cache_dir)
        except Exception as exc:
            print(f"  [{i}/{len(sessions)}] {sess['roman']}: ERR {exc}")
            continue
        print(f"  [{i}/{len(sessions)}] sesja {sess['roman']}: {len(votes_meta)} głosowań")
        if args.dry_run:
            continue
        for vm in votes_meta:
            try:
                html = fetch_text(vm["url"], cache_dir=cache_dir)
            except Exception:
                continue
            parsed = parse_voting_page(html, vm["url"])
            if parsed and parsed["session_date"]:
                parsed["topic"] = vm["topic"]
                parsed["session_number"] = sess["roman"]
                all_votes.append(parsed)

    if args.dry_run:
        return 0

    print(f"\n[3/3] Buduj output z {len(all_votes)} głosowań...")
    councilors, name_to_idx = build_councilor_index(all_votes)
    indexed = [votes_to_index(v, v["topic"], v["session_number"], name_to_idx) for v in all_votes]
    out_sessions = aggregate_sessions([{**v, "session_number": v["session_number"]} for v in all_votes])

    output = {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "sessions": out_sessions,
        "total_sessions": len(out_sessions),
        "total_votes": len(indexed),
        "total_councilors": len(councilors),
        "councilors": [],
        "votes": indexed,
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilors,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  {len(out_sessions)} sesji, {len(indexed)} głosowań, {len(councilors)} radnych")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
