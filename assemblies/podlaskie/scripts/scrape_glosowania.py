#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Podlaskiego, VII kadencja 2024-2029.

Źródło: bip.podlaskie.eu/wojewodztwo/wladze/sejmik_woj/gosowania_z_sesji_sejmiku/

Pipeline:
1. Per rok: ?p=VII+kadencja+(2024-2029)^YYYY → lista linków do per-sesja
   podstron typu `wykazy-glosowan-radnych-z-sesji-sejmiku-{rzymski}-sesja-...-z-dnia-YYYY-MM-DD.html`
2. Per sesja podstrona: pobierz HTML, znajdź link do PDF w /resource/{id}/{n}/
3. Każdy PDF zawiera wszystkie głosowania sesji (multi-page), wygenerowane
   przez app.esesja.pl. Format identyczny jak śląski — strony zawierają:

      Wygenerowano za pomocą app.esesja.pl
      Wyniki głosowania
      ZA: N, PRZECIW: N, WSTRZYMUJĘ SIĘ: N, BRAK GŁOSU: N, NIEOBECNI: N
      Wyniki imienne:
      ZA (N)
      Imie NAZWISKO, Imie NAZWISKO, ...
      PRZECIW (N)
      ...

Multi-vote na stronę: czasem jedna strona zawiera koniec poprzedniego głosowania
i początek następnego. Parsujemy regex per "Wyniki głosowania" header.

Output: schemat zgodny z mazowieckim/podkarpackim.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


BASE = "https://bip.podlaskie.eu"
LISTING_BASE = f"{BASE}/wojewodztwo/wladze/sejmik_woj/gosowania_z_sesji_sejmiku/"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
USER_AGENT = "Mozilla/5.0 Radoskop/1.0"
TIMEOUT = 30
SLEEP = 0.1


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


ROMAN_RE = re.compile(r"-(\w+?)-sesja-")


def discover_sessions(cache_dir: Path | None) -> list[dict]:
    """Per rok 2024-2026, zbierz linki sesji."""
    out: list[dict] = []
    seen = set()
    for year in [2024, 2025, 2026]:
        p_value = f"VII+kadencja+%282024-2029%29%5E{year}"
        url = f"{LISTING_BASE}?p={p_value}"
        try:
            text = fetch_text(url, cache_dir)
        except Exception:
            continue
        # Linki: href="/wojewodztwo/wladze/sejmik_woj/gosowania_z_sesji_sejmiku/wykazy-glosowan-radnych-..."
        paths = re.findall(
            r'href="(/wojewodztwo/wladze/sejmik_woj/gosowania_z_sesji_sejmiku/wykazy-glosowan-radnych[^"]+)"',
            text,
        )
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            # Dodaj .html jeśli brak (BIP wymaga rozszerzenia)
            full = p if p.endswith(".html") else p + ".html"
            url_full = f"{BASE}{full}"
            roman_m = re.search(r"sejmiku-([a-z]+)-sesja", p)
            roman = roman_m.group(1).upper() if roman_m else ""
            date_m = re.search(r"(\d{4}-\d{2}-\d{2})", p)
            date_iso = date_m.group(1) if date_m else ""
            out.append({"url": url_full, "roman": roman, "date_hint": date_iso})
    return out


def fetch_session_pdf_url(session_url: str, cache_dir: Path | None) -> str:
    """Z per-sesja podstrony znajdź URL do PDF (resource/{id}/{n}/...pdf)."""
    text = fetch_text(session_url, cache_dir)
    m = re.search(r'href="(https?://bip\.podlaskie\.eu/resource/\d+/\d+/[^"]+\.pdf)"', text)
    if m:
        return m.group(1)
    # Próbuj też relative
    m = re.search(r'href="(/resource/\d+/\d+/[^"]+\.pdf)"', text)
    if m:
        return f"{BASE}{m.group(1)}"
    return ""


# ---------------------------------------------------------------------------
# Parse PDF (format app.esesja.pl, multi-page = wiele głosowań)
# ---------------------------------------------------------------------------

VOTE_CATEGORIES = ["ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ", "BRAK GŁOSU", "NIEOBECNI"]
CAT_TO_KEY = {
    "ZA": "za", "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu", "NIEOBECNI": "nieobecni",
}


def _load_allowlist() -> set[str]:
    """Wczytaj 30 radnych z config.json (PKW). Allowlist filtruje hałas
    pypdf-a (rozbicia tokenów, sklejone kawałki kolejnych głosowań).
    """
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.is_file():
        return set()
    try:
        return set(json.loads(cfg.read_text(encoding="utf-8")).get("club_assignments", {}))
    except Exception:
        return set()


_ALLOWLIST: set[str] = set()
_ALLOWLIST_SURNAMES: dict[str, str] = {}  # surname (upper, no space) → full


def parse_voting_block(text: str, session_date: str) -> dict | None:
    """Parsuj pojedynczy blok 'Wyniki głosowania' z PDF."""
    counts = {k: 0 for k in CAT_TO_KEY.values()}
    m = re.search(
        r"ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ\s+SIĘ:\s*(\d+),\s*BRAK\s+GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)",
        text,
    )
    if not m:
        return None
    counts["za"] = int(m.group(1))
    counts["przeciw"] = int(m.group(2))
    counts["wstrzymal_sie"] = int(m.group(3))
    counts["brak_glosu"] = int(m.group(4))
    counts["nieobecni"] = int(m.group(5))

    # Topic — przed "Głosowano w sprawie:" lub "Wyniki głosowania"
    topic = ""
    tm = re.search(r"Głosowano w sprawie:\s*(.+?)\s+Wyniki głosowania", text, re.DOTALL)
    if tm:
        topic = re.sub(r"\s+", " ", tm.group(1)).strip()

    # Imienne
    names_by_cat: dict[str, list[str]] = {k: [] for k in counts}
    after = text.split("Wyniki imienne:", 1)
    if len(after) == 2:
        details = after[1]
        cat_pat = "|".join(re.escape(c) for c in VOTE_CATEGORIES)
        for m in re.finditer(rf"({cat_pat})\s*\(\d+\)\s*(.*?)(?=(?:{cat_pat})\s*\(|Wyniki głosowania|$)", details, re.DOTALL):
            label = m.group(1).upper()
            chunk = m.group(2).strip()
            key = CAT_TO_KEY.get(label)
            if not key or not chunk:
                continue
            # imiona oddzielone przecinkami
            for name in re.split(r",\s*", chunk):
                name = re.sub(r"\s+", " ", name.strip())
                if name and len(name.split()) >= 2:
                    # Normalizuj: PODLASKIE używa "Imie NAZWISKO". Bierz tylko
                    # PIERWSZE imię + nazwisko (skip middle), żeby było zgodne
                    # z PKW config_assignments format.
                    # Po pypdf mogą być wewnętrzne spacje w słowie:
                    # "N Aszkiewicz" → "Naszkiewicz". Sklej tokeny które
                    # zaczynają się 1-literowo + Title.
                    parts = name.split()
                    # Sklej pojedyncze litery z następnym tokenem
                    merged = []
                    i = 0
                    while i < len(parts):
                        if len(parts[i]) == 1 and i + 1 < len(parts):
                            merged.append(parts[i] + parts[i+1].lower())
                            i += 2
                        else:
                            merged.append(parts[i])
                            i += 1
                    parts = merged
                    surname_parts = [p for p in parts if p.isupper() or all(c.isupper() or not c.isalpha() for c in p)]
                    first_parts = [p for p in parts if p not in surname_parts]
                    if surname_parts and first_parts:
                        normalized = f"{first_parts[0]} {' '.join(s.title() for s in surname_parts)}"
                    else:
                        normalized = " ".join(parts).title()
                    # Match against allowlist (30 radnych z PKW). Match po
                    # surname (case-insensitive, strip spaces). Pominięcia
                    # eliminują pypdf hałas typu "Adam Sekściński 11. 154/.".
                    global _ALLOWLIST_SURNAMES
                    if not _ALLOWLIST_SURNAMES:
                        for full_name in _load_allowlist():
                            sname_key = full_name.split()[-1].upper().replace(" ", "")
                            _ALLOWLIST_SURNAMES[sname_key] = full_name
                    norm_surname = normalized.split()[-1].upper().replace(" ", "") if normalized else ""
                    canonical = _ALLOWLIST_SURNAMES.get(norm_surname)
                    if not canonical:
                        continue  # nie pasuje — pominięte (hałas)
                    if canonical not in names_by_cat[key]:
                        names_by_cat[key].append(canonical)

    return {
        "session_date": session_date,
        "topic": topic,
        "counts": counts,
        "named_votes": names_by_cat,
    }


def parse_session_pdf(pdf_bytes: bytes, session_date: str, source_url: str) -> list[dict]:
    """Multi-vote PDF — parsuj wszystkie bloki 'Wyniki głosowania'."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print("UWAGA: pip install pypdf", file=sys.stderr)
        raise
    reader = PdfReader(io.BytesIO(pdf_bytes))
    full_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    # Normalize
    flat = re.sub(r"\s+", " ", full_text)
    # Każde głosowanie zaczyna się od numeru + "Głosowano w sprawie:"
    # Split na "Głosowano w sprawie:" sekcje
    sections = re.split(r"(?=Głosowano w sprawie:)", flat)
    votes = []
    for sec in sections:
        if "Wyniki głosowania" not in sec or "Wyniki imienne" not in sec:
            continue
        # Pomiń pierwszą sekcję jeśli to lista "Obecni:" (BEZ "Głosowano w sprawie")
        if not sec.startswith("Głosowano w sprawie"):
            continue
        v = parse_voting_block(sec, session_date)
        if v:
            v["source_url"] = source_url
            v["voted_at"] = f"{session_date}T00:00:00"  # PDF nie podaje godziny per glosowanie
            votes.append(v)
    return votes


def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen = set()
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
        "session_number": vote.get("session_number", ""),
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


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        if not d: continue
        sess = by_date.setdefault(d, {
            "date": d, "number": v.get("session_number", ""),
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cache = Path(args.cache_dir) if args.cache_dir else None
    print(f"=== Radoskop Sejmik Podlaski ({BASE}) ===\n")
    print("[1/3] Discover sessions...")
    sessions = discover_sessions(cache)
    print(f"  Znaleziono {len(sessions)} sesji")
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    print(f"\n[2/3] Pobieranie PDFów {len(sessions)} sesji...")
    all_votes = []
    for i, sess in enumerate(sessions, 1):
        try:
            pdf_url = fetch_session_pdf_url(sess["url"], cache)
        except Exception as exc:
            print(f"  [{i}/{len(sessions)}] {sess['roman']}: ERR {exc}")
            continue
        if not pdf_url:
            print(f"  [{i}/{len(sessions)}] {sess['roman']}: brak PDF")
            continue
        print(f"  [{i}/{len(sessions)}] sesja {sess['roman']} ({sess['date_hint']})")
        if args.dry_run:
            continue
        try:
            pdf_bytes = fetch_bytes(pdf_url, cache)
            votes = parse_session_pdf(pdf_bytes, sess["date_hint"], pdf_url)
            print(f"    {len(votes)} głosowań")
            for v in votes:
                v["session_number"] = sess["roman"]
                all_votes.append(v)
        except Exception as exc:
            print(f"    PDF err: {exc}")

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
