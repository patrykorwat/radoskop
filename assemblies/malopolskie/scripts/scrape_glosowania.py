#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Małopolskiego, kadencja 2024-2029.

BIP małopolski (bip.malopolska.pl) używa Madkom CMS, tego samego co
pomorski (`bip_kind: madkom_cms_rest`). Różnica: małopolski publikuje
imienne wyniki jako osobne docx-y per uchwała (nie jeden zbiorczy PDF).

Struktura menu BIP:
  menu 433528 = "Wyniki głosowań / 2024-2029"
    article {N} = "Głosowania podczas XXIX Sesji ..." (per sesja)
      attachments[] = ~25 docx (po jednym na uchwałę)
  menu 433527 = "Protokoły z sesji sejmiku / 2024-2029"
    (prozaiczny content, NIE używamy bo brak imiennych)

Endpointy Madkom REST API (te same co pomorski):
  GET /api/menu/{menu_id}/articles?limit=N&offset=0&archived=0
  GET /api/articles/{article_id}
  GET /e,pobierz,get.html?id={attachment_id}  (download docx)

Treść docx jest identyczna z eSesja PDF standard, więc używamy
`lib_voting_pdf_table.parse_voting_docx`.

Output: kadencja-2024-2029.json w STANDARDOWEJ schemie sejmików (czytanej
przez build_assembly_metrics.py + frontend SPA):
  {
    "id": "2024-2029",
    "councilor_index": ["Imię Nazwisko", ...],   # posortowane, unikalne
    "votes": [{                                    # PŁASKA lista
        "id", "session_date", "session_number", "topic", "counts",
        "named_votes": {kategoria: [indeksy do councilor_index]},
        "voted_at", "source_url", "druk", "resolution"
    }, ...],
    "sessions": [{
        "number": "XXIX", "date": "2026-04-27", "vote_count": 25,
        "attendees": [...], "attendee_count": 39, "speakers": []
    }],
    "councilors": []   # statystyki dolicza build_assembly_metrics.py
  }

UWAGA HISTORYCZNA: wcześniej scraper emitował nietypową schemę (klucz
`session_number`, votes zagnieżdżone w sesji, councilors jako nazwiska bez
councilor_index). build_assembly_metrics czyta `votes`/`councilor_index`/
`sessions[].number`/`attendees`, więc liczył 0 radnych a strona
/term/.../sessions/ renderowała się pusto.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Wstawiamy radoskop/scripts/ do sys.path żeby importować lib_voting_pdf_table.
SCRIPT_DIR = Path(__file__).resolve().parent
RADOSKOP_SCRIPTS = SCRIPT_DIR.parent.parent.parent / "scripts"
if str(RADOSKOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(RADOSKOP_SCRIPTS))

from lib_voting_pdf_table import parse_voting_docx, validate_parsed  # noqa: E402


BASE = "https://bip.malopolska.pl"
WYNIKI_GLOSOWAN_MENU_ID = 433528  # "Wyniki głosowań / 2024-2029"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------


def fetch(url: str, *, cache_dir: Path | None = None, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = resp.read()
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GET {url} failed: {e}") from e
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    time.sleep(SLEEP_BETWEEN)
    return data


def fetch_json(url: str, *, cache_dir: Path | None = None) -> Any:
    return json.loads(fetch(url, cache_dir=cache_dir, suffix=".json").decode("utf-8"))


# ---------------------------------------------------------------------------
# Discovery: lista sesji + docx attachments per sesja
# ---------------------------------------------------------------------------


def discover_sessions(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Zwraca listę sesji VII kadencji z artykułów Madkom.

    Format zwracany: lista dictów
      {"article_id", "session_number" (rzymski np. "XXIX"), "date" (ISO),
       "title"}
    """
    list_url = (
        f"{BASE}/api/menu/{WYNIKI_GLOSOWAN_MENU_ID}/articles"
        "?limit=200&offset=0&archived=0"
    )
    listing = fetch_json(list_url, cache_dir=cache_dir)
    articles = listing.get("articles") or []

    print(f"==> Sesji w menu {WYNIKI_GLOSOWAN_MENU_ID}: {len(articles)}", file=sys.stderr)

    sessions = []
    for art in articles:
        art_id = art.get("id")
        if not art_id:
            continue
        # Title i data w columnFields/aliasFields
        column_fields = {f["fieldId"]: f["value"] for f in art.get("columnFields", [])}
        alias_fields = {f["alias"]: f["value"] for f in art.get("aliasFields", [])}
        title = alias_fields.get("title") or column_fields.get("title", "")
        active_ymd = column_fields.get("activeYMD", "")

        # Wyciągnij numer sesji rzymski z tytułu
        # "Głosowania podczas XXIX Sesji Sejmiku Województwa Małopolskiego w dniu 27 kwietnia 2026 r."
        session_match = re.search(r"podczas\s+([IVXLCDM]+)\s+Sesj", title)
        session_number = session_match.group(1) if session_match else None

        # Data sesji z tytułu: "w dniu 27 kwietnia 2026 r."
        date_iso = _parse_session_date(title)

        sessions.append({
            "article_id": art_id,
            "session_number": session_number,
            "date": date_iso,
            "title": title,
        })
    return sessions


_POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}


def _parse_session_date(title: str) -> str | None:
    """Wyciąga ISO date z tytułu sesji w formacie 'w dniu DD miesiąc YYYY'."""
    m = re.search(
        r"w dniu\s+(\d{1,2})\s+("
        + "|".join(_POLISH_MONTHS.keys())
        + r")\s+(\d{4})",
        title,
    )
    if not m:
        return None
    d, mname, y = m.groups()
    return f"{y}-{_POLISH_MONTHS[mname]:02d}-{int(d):02d}"


def discover_session_attachments(
    article_id: int, *, cache_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Zwraca listę docx attachments z artykułu sesji.

    Każdy element: {"id", "name", "size", "url"}
    """
    art_url = f"{BASE}/api/articles/{article_id}"
    art = fetch_json(art_url, cache_dir=cache_dir)
    attachments = art.get("attachments") or []

    results = []
    for att in attachments:
        if not att.get("downloadable", True):
            continue
        ext = (att.get("extension") or "").lower()
        if ext != "docx":
            continue
        att_id = att.get("id")
        if att_id is None:
            continue
        results.append({
            "id": att_id,
            "name": att.get("name", ""),
            "size": att.get("size"),
            "url": f"{BASE}/e,pobierz,get.html?id={att_id}",
        })
    return results


# ---------------------------------------------------------------------------
# Parse: pobierz docx + przekaż do lib_voting_pdf_table
# ---------------------------------------------------------------------------


def parse_session_docxs(
    attachments: list[dict[str, Any]], cache_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Pobierz wszystkie docx z attachments i parsuje każdy do voting record.

    Returns: lista głosowań (zwykle 1 docx = 1 vote, ale czasem są wnioski
    proceduralne też jako docxy).
    """
    votes = []
    for att in attachments:
        try:
            data = fetch(att["url"], cache_dir=cache_dir, suffix=".docx")
        except Exception as e:
            print(f"  WARN: nie pobrano {att['name'][:60]}: {e}", file=sys.stderr)
            continue

        # Zapisz tymczasowo i parsuj
        tmp_path = (cache_dir or Path("/tmp")) / f"_tmp_{att['id']}.docx"
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(data)
        try:
            session = parse_voting_docx(tmp_path)
            if session["votes"]:
                v = session["votes"][0]
                v["source_attachment_id"] = att["id"]
                v["source_attachment_name"] = att["name"]
                votes.append(v)
            else:
                print(f"  WARN: docx {att['id']} = 0 głosowań ({att['name'][:60]})",
                      file=sys.stderr)
        except Exception as e:
            print(f"  WARN: parse {att['id']}: {e}", file=sys.stderr)
        finally:
            if not cache_dir:
                tmp_path.unlink(missing_ok=True)
    return votes


# ---------------------------------------------------------------------------
# Main: build kadencja JSON
# ---------------------------------------------------------------------------


# Kategorie głosu (nieobecni = nieobecny; reszta = obecny).
_VOTE_CATS = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")
# Obecny na sesji = oddał głos w jakiejkolwiek kategorii poza nieobecni.
_PRESENT_CATS = ("za", "przeciw", "wstrzymal_sie", "brak_glosu")


def build_kadencja(cache_dir: Path | None = None,
                   limit_sessions: int | None = None) -> dict[str, Any]:
    """Buduje plik kadencji w STANDARDOWEJ schemie sejmików, czytanej przez
    build_assembly_metrics.py i frontend SPA:

      - `councilor_index`: posortowana lista unikalnych nazwisk
      - `votes`: PŁASKA lista głosowań; `named_votes` to {kategoria: [indeksy]}
        wskazujące do councilor_index (nie nazwiska)
      - `sessions`: {number, date, vote_count, attendees, attendee_count,
        speakers} — klucz `number` (nie `session_number`!), z listą obecnych
        do liczenia frekwencji

    Wcześniej scraper emitował nietypową schemę (session_number + zagnieżdżone
    votes + councilors jako nazwiska bez councilor_index), przez co
    build_assembly_metrics liczyło 0 radnych, a strona /term/.../sessions/
    renderowała się pusta. Patrz git blame / pamięć projektu.
    """
    sessions = discover_sessions(cache_dir=cache_dir)
    if limit_sessions:
        sessions = sessions[:limit_sessions]

    # Pass 1: pobierz i sparsuj wszystkie głosowania (named_votes = nazwiska),
    # zbierz pełen zbiór radnych do indeksu.
    parsed: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    name_set: set[str] = set()
    for sess in sessions:
        print(f"\n=> Sesja {sess['session_number']} ({sess['date']}) "
              f"art={sess['article_id']}", file=sys.stderr)
        attachments = discover_session_attachments(
            sess["article_id"], cache_dir=cache_dir
        )
        print(f"   docx attachments: {len(attachments)}", file=sys.stderr)
        votes = parse_session_docxs(attachments, cache_dir=cache_dir)
        for v in votes:
            for cat in _VOTE_CATS:
                for name in v.get("named_votes", {}).get(cat, []):
                    name_set.add(name)
        parsed.append((sess, votes))

    councilor_index = sorted(name_set)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    # Pass 2: spłaszcz do top-level votes (indeksy) + zbuduj sesje z obecnością.
    top_votes: list[dict[str, Any]] = []
    out_sessions: list[dict[str, Any]] = []
    seq = 0
    for sess, votes in parsed:
        attendees: set[str] = set()
        for v in votes:
            named_idx = {
                cat: sorted(
                    name_to_idx[n]
                    for n in v.get("named_votes", {}).get(cat, [])
                    if n in name_to_idx
                )
                for cat in _VOTE_CATS
            }
            for cat in _PRESENT_CATS:
                for n in v.get("named_votes", {}).get(cat, []):
                    attendees.add(n)
            att_id = v.get("source_attachment_id")
            top_votes.append({
                "id": f"{sess['date']}_{seq}",
                "session_date": sess["date"],
                "session_number": sess["session_number"],
                "source_url": (
                    f"{BASE}/e,pobierz,get.html?id={att_id}" if att_id else ""
                ),
                "topic": v.get("topic", ""),
                "druk": None,
                "resolution": None,
                "counts": v.get("counts", {}),
                "named_votes": named_idx,
                "voted_at": v.get("voted_at", ""),
            })
            seq += 1
        out_sessions.append({
            "number": sess["session_number"],
            "date": sess["date"],
            "vote_count": len(votes),
            "attendees": sorted(attendees),
            "attendee_count": len(attendees),
            "speakers": [],
        })

    # Sesje chronologicznie malejąco (najnowsza pierwsza) — listing Madkom
    # bywa nieuporządkowany.
    out_sessions.sort(key=lambda s: (s.get("date") or "", s.get("number") or ""),
                      reverse=True)

    return {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "councilors": [],
        "councilor_index": councilor_index,
        "sessions": out_sessions,
        "votes": top_votes,
        "total_sessions": len(out_sessions),
        "total_votes": len(top_votes),
        "total_councilors": len(councilor_index),
        "similarity_top": [],
        "similarity_bottom": [],
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"{BASE}/api/menu/{WYNIKI_GLOSOWAN_MENU_ID}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Sejmik Województwa Małopolskiego")
    parser.add_argument("--cache", type=Path, default=Path(".cache/malopolskie"),
                        help="Cache directory dla HTTP i docx")
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("docs/kadencja-2024-2029.json"),
                        help="Output JSON")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit sesji (debug)")
    args = parser.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    kadencja = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit)

    with args.output.open("w", encoding="utf-8") as f:
        json.dump(kadencja, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved {args.output}", file=sys.stderr)
    print(f"  Sesji: {kadencja['total_sessions']}", file=sys.stderr)
    print(f"  Głosowań: {kadencja['total_votes']}", file=sys.stderr)
    print(f"  Radnych: {kadencja['total_councilors']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
