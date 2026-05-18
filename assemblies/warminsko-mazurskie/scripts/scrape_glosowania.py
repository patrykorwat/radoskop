#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Warmińsko-Mazurskiego, VII kad. 2024-2029.

Źródło: bip.warmia.mazury.pl/kategoria/116/imienne-wykazy-glosowan.html

Pipeline:
1. Listing per rok: /3970/imienne-wykazy-glosowan-...-w-2026-roku.html (i analogiczne dla 2024, 2025)
2. Każdy rok ma linki "Imienny wykaz głosowań {ROMAN} sesja Sejmiku, DD.MM.YYYY" do
   /attachment/informacja/{id}/{hash}.html (Content-Disposition: attachment, redirect na PDF)
3. PDF zawiera N stron, każda strona = jedno głosowanie z 2-kolumnową tabelą
   imienną (jak w podkarpackim):

      Wydrukowano: DD.MM.YYYY HH:MM:SS
      {N} {ROMAN} Sesja VII kadencji Sejmiku Województwa Warmińsko-Mazurskiego
      Głosowanie {seq}. {topic}
      Typ głosowania jawne  Data głosowania: DD.MM.YYYY HH:MM
      Liczba uprawnionych N    Głosy za N
      Liczba obecnych N        Głosy przeciw N
      Liczba nieobecnych N     Głosy wstrzymujące się N
                               Obecni niegłosujący N
      Kworum zostało osiągnięte
      Uprawnieni do głosowania
      Lp. Nazwisko i imię Głos Lp. Nazwisko i imię Głos
      1.  Andruszkiewicz Piotr  WSTRZYMUJĘ SIĘ  16. Kuchciński Marcin ZA
      ...

Output: schemat zgodny z mazowieckim/podkarpackim.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


BASE = "https://bip.warmia.mazury.pl"
# Hardcoded ID-y per rok (z probe 2026-05-18)
YEAR_LISTING_IDS = {2024: 3257, 2025: 3492, 2026: 3970}
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
USER_AGENT = "Mozilla/5.0 Radoskop/1.0"
TIMEOUT = 30
SLEEP = 0.1

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


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
    with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
        data = r.read()
    if cache:
        cache.write_bytes(data)
    time.sleep(SLEEP)
    return data


def fetch_text(url: str, cache_dir: Path | None) -> str:
    return fetch_bytes(url, cache_dir).decode("utf-8", "replace")


def discover_sessions(cache_dir: Path | None) -> list[dict]:
    """Per rok: pobierz listing → linki attachment do PDF + ekstrahuj datę."""
    out = []
    for year, listing_id in YEAR_LISTING_IDS.items():
        url = f"{BASE}/{listing_id}/imienne-wykazy-glosowan-z-obrad-sesji-sejmiku-w-{year}-roku.html"
        try:
            text = fetch_text(url, cache_dir)
        except Exception as exc:
            print(f"  rok {year}: ERR {exc}")
            continue
        # Linki do attachment/informacja/{id}/{hash}.html z opisem "Imienny wykaz głosowań {ROMAN} sesja Sejmiku, DD.MM.YYYY"
        for m in re.finditer(
            r'<a[^>]+href="(/attachment/informacja/\d+/[^"]+\.html)"[^>]*>([^<]*Imienny\s+wykaz\s+głosowań[^<]+)</a>',
            text,
        ):
            path, label = m.group(1), m.group(2).strip()
            # Wyciągnij rzymski + datę
            rm = re.search(r"(XX[IVX]*|X[IVX]+|[IVX]+)\s+sesja", label)
            roman = rm.group(1).upper() if rm else ""
            dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", label)
            date_iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
            out.append({
                "url": f"{BASE}{path}",
                "roman": roman,
                "date": date_iso,
                "year": year,
            })
    return out


# ---------------------------------------------------------------------------
# Parse PDF
# ---------------------------------------------------------------------------

VOTE_LABELS_ORDERED = ["WSTRZYMUJĘ SIĘ", "WSTRZYMUJE SIĘ", "NIEOBECNY", "NIEOBECNA",
                      "BRAK GŁOSU", "ZA", "PRZECIW"]
CAT_TO_KEY = {
    "ZA": "za", "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
    "BRAK GŁOSU": "brak_glosu",
}


def _load_allowlist_surnames() -> dict[str, str]:
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.is_file():
        return {}
    try:
        names = json.loads(cfg.read_text(encoding="utf-8")).get("club_assignments", {})
    except Exception:
        return {}
    out = {}
    for full in names:
        sname = full.split()[-1].upper().replace(" ", "").replace("-", "")
        out[sname] = full
    return out


_ALLOWLIST: dict[str, str] = {}


def parse_voting_page(text: str, session_date: str, source_url: str) -> dict | None:
    """Parsuj jedną stronę PDF — jedno głosowanie."""
    if "PROTOKÓŁ GŁOSOWANIA" in text or "Głosowanie" not in text:
        # Pomiń strony bez "Głosowanie {seq}." (np. cover page)
        if "Głosowanie" not in text:
            return None

    # Counts
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    for pat, key in [
        (r"Głosy\s+za\s+(\d+)", "za"),
        (r"Głosy\s+przeciw\s+(\d+)", "przeciw"),
        (r"Głosy\s+wstrzymujące\s+się\s+(\d+)", "wstrzymal_sie"),
        (r"Obecni\s+niegłosujący\s+(\d+)", "brak_glosu"),
        (r"Liczba\s+nieobecnych\s+(\d+)", "nieobecni"),
    ]:
        m = re.search(pat, text)
        if m:
            counts[key] = int(m.group(1))

    # Data głosowania
    voted_at = ""
    m = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        d, mo, y, h, mi = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        voted_at = f"{y}-{mo}-{d}T{int(h):02d}:{mi}:00"

    # Topic
    topic = ""
    m = re.search(r"Głosowanie\s+(\d+)\.\s*(.+?)\s+Typ\s+głosowania", text, re.DOTALL)
    if m:
        topic = re.sub(r"\s+", " ", m.group(2)).strip()

    # Imienne — sekcja po "Uprawnieni do głosowania"
    names_by_cat: dict[str, list[str]] = {k: [] for k in counts}
    global _ALLOWLIST
    if not _ALLOWLIST:
        _ALLOWLIST = _load_allowlist_surnames()
    after = text.split("Uprawnieni do głosowania", 1)
    if len(after) == 2:
        details = after[1]
        # Match każde nazwisko z allowlist + następujący GŁOS
        flat = re.sub(r"\s+", " ", details)
        for sname_key, full_name in _ALLOWLIST.items():
            # Surname może być w PDF z myślnikiem ("Astramowicz-Leyk") lub spacją
            # Stwórz wzorzec który matchuje wariacje
            sname_pattern = full_name.split()[-1].replace("-", r"\s*-?\s*")
            # Match: nazwisko, potem spacje, potem GŁOS
            pat = sname_pattern + r"\s+(?:[A-ZŁŚĄĘĆŃÓŻŹ][a-złśąęćńóżź]+)?\s*(ZA|PRZECIW|WSTRZYMUJĘ\s+SIĘ|WSTRZYMUJE\s+SIĘ|NIEOBECNY|NIEOBECNA|BRAK\s+GŁOSU)"
            m = re.search(pat, flat)
            if m:
                label = re.sub(r"\s+", " ", m.group(1).upper())
                cat = CAT_TO_KEY.get(label)
                if cat and full_name not in names_by_cat[cat]:
                    names_by_cat[cat].append(full_name)

    return {
        "session_date": session_date,
        "voted_at": voted_at,
        "topic": topic,
        "counts": counts,
        "named_votes": names_by_cat,
        "source_url": source_url,
    }


def parse_pdf(pdf_bytes: bytes, session_date: str, source_url: str) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    votes = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        v = parse_voting_page(text, session_date, f"{source_url}#page={i}")
        if v and v["voted_at"]:
            votes.append(v)
    return votes


# ---------------------------------------------------------------------------
# Orchestracja
# ---------------------------------------------------------------------------

def build_councilor_index(votes):
    seen = set()
    for v in votes:
        for names in v["named_votes"].values():
            for n in names:
                seen.add(n)
    sorted_names = sorted(seen)
    return sorted_names, {n: i for i, n in enumerate(sorted_names)}


def votes_to_index(vote, idx, seq):
    return {
        "id": f"{vote['session_date']}_{seq}",
        "session_date": vote["session_date"],
        "session_number": vote.get("session_roman", ""),
        "source_url": vote["source_url"],
        "topic": vote["topic"],
        "druk": None, "resolution": None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(idx[n] for n in names if n in idx)
            for cat, names in vote["named_votes"].items()
        },
        "voted_at": vote["voted_at"],
    }


def aggregate_sessions(votes):
    by_date = {}
    for v in votes:
        d = v["session_date"]
        if not d: continue
        sess = by_date.setdefault(d, {
            "date": d, "number": v.get("session_roman", ""),
            "vote_count": 0, "attendees": set(), "attendee_count": 0, "speakers": [],
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cache = Path(args.cache_dir) if args.cache_dir else None
    print(f"=== Radoskop Sejmik Warmińsko-Mazurski ===\n")
    print("[1/3] Discover sessions per year...")
    sessions = discover_sessions(cache)
    print(f"  Znaleziono {len(sessions)} sesji")
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    print(f"\n[2/3] Pobieranie {len(sessions)} PDFów...")
    all_votes = []
    for i, sess in enumerate(sessions, 1):
        print(f"  [{i}/{len(sessions)}] {sess['roman']} sesja ({sess['date']})")
        if args.dry_run:
            continue
        try:
            pdf_bytes = fetch_bytes(sess["url"], cache)
            votes = parse_pdf(pdf_bytes, sess["date"], sess["url"])
            print(f"    {len(votes)} głosowań")
            for v in votes:
                v["session_roman"] = sess["roman"]
                all_votes.append(v)
        except Exception as exc:
            print(f"    ERR: {exc}")

    if args.dry_run:
        return 0

    print(f"\n[3/3] Buduj output z {len(all_votes)} głosowań...")
    councilors, idx = build_councilor_index(all_votes)
    print(f"  Radnych: {len(councilors)}")
    indexed = [votes_to_index(v, idx, i) for i, v in enumerate(all_votes)]
    out_sessions = aggregate_sessions(all_votes)

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
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {len(out_sessions)} sesji, {len(indexed)} głosowań, {len(councilors)} radnych")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
