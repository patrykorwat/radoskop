#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Lubelskiego, kadencja 2024-2029.

BIP lubelski (umwl.bip.lubelskie.pl) **nie publikuje publicznego indeksu
sesji VII kadencji**. Lista kategorii (id=55, id=1066, id=1285 VII KADENCJA)
zawiera tylko archiwalne sesje 2018-2020. Indeks 6 widocznych document_id
na id=1066 wskazuje inne sprawy BIP, nie sesje sejmiku.

Pojedyncza sesja VII kadencji jest dostępna pod URL pattern:
  https://umwl.bip.lubelskie.pl/index.php?id=1066&action=details&document_id={did}
ale brak listy document_id. Workaround: znamy URL pattern PDF który jest
predykcyjny:
  https://umwl.bip.lubelskie.pl/upload/pliki/raport_z_glosowan_-_{roman_lower}_sesja_sejmiku_wojewodztwa_lubelskiego.pdf

Enumerujemy rzymskimi I-XXX, sprawdzamy first bytes każdego URL (musi
zaczynać się od `%PDF-`, inaczej serwer zwraca HTML 200 z error page).

Format PDF: eSesja standard (potwierdzone XXI sesja 16/17 walidacja OK).
Używamy `lib_voting_pdf_table.parse_voting_pdf`.

Output: kadencja-2024-2029.json zgodne ze schemą innych sejmików.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
RADOSKOP_SCRIPTS = SCRIPT_DIR.parent.parent.parent / "scripts"
if str(RADOSKOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(RADOSKOP_SCRIPTS))

from lib_voting_pdf_table import parse_voting_pdf, validate_parsed  # noqa: E402


BASE = "https://umwl.bip.lubelskie.pl"
PDF_URL_TEMPLATE = (
    BASE + "/upload/pliki/raport_z_glosowan_-_{roman_lower}_sesja_sejmiku_"
    "wojewodztwa_lubelskiego.pdf"
)
MAX_SESSION_NUMBER = 30  # próbujemy I-XXX, ale realnie >25 jest mało prawdopodobne

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05

# Niektóre certy mogą być przeterminowane na BIP urzędów - safe default
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

ROMAN_NUMERALS = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII",
    8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII", 13: "XIII",
    14: "XIV", 15: "XV", 16: "XVI", 17: "XVII", 18: "XVIII", 19: "XIX",
    20: "XX", 21: "XXI", 22: "XXII", 23: "XXIII", 24: "XXIV", 25: "XXV",
    26: "XXVI", 27: "XXVII", 28: "XXVIII", 29: "XXIX", 30: "XXX",
}


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------


def fetch_pdf(url: str, *, cache_dir: Path | None = None) -> bytes | None:
    """Pobiera URL i zwraca bytes JEŚLI to PDF (first bytes %PDF-), inaczej None."""
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + ".pdf")
        if cache_path.is_file():
            data = cache_path.read_bytes()
            return data if data[:4] == b"%PDF" else None

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT, context=SSL_CTX) as resp:
            data = resp.read()
    except (HTTPError, URLError) as e:
        return None

    time.sleep(SLEEP_BETWEEN)
    if data[:4] != b"%PDF":
        # Serwer zwraca 200 z HTML error page dla nieistniejących plików.
        return None
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# Discovery: enumeracja rzymskich numerów sesji
# ---------------------------------------------------------------------------


def discover_session_pdfs(cache_dir: Path | None = None,
                          max_number: int = MAX_SESSION_NUMBER) -> list[dict[str, Any]]:
    """Enumeruje URL pattern, zwraca listę istniejących sesji.

    Each: {"session_number" (rzymski), "session_arabic", "pdf_url", "pdf_data"}
    """
    # Pełna enumeracja I-MAX. Pierwsze inauguracyjne sesje VII kadencji
    # zwykle nie są publikowane bo zawierały tylko proceduralne uchwały;
    # numeracja PDFów zaczyna się od V/VI sesji. Nie używamy early-stop bo
    # naming może mieć dziury (znaleziono XVII i XXI, brak XVIII/XIX/XX
    # przy tym wzorcu - prawdopodobnie nazewnictwo plików niespójne).
    sessions = []
    for arabic in range(1, max_number + 1):
        roman = ROMAN_NUMERALS[arabic]
        roman_lower = roman.lower()
        url = PDF_URL_TEMPLATE.format(roman_lower=roman_lower)
        data = fetch_pdf(url, cache_dir=cache_dir)
        if data is None:
            continue
        print(f"   {roman} ({arabic}): {len(data)} bytes", file=sys.stderr)
        sessions.append({
            "session_number": roman,
            "session_arabic": arabic,
            "pdf_url": url,
            "pdf_data": data,
        })
    return sessions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_kadencja(cache_dir: Path | None = None,
                   limit_sessions: int | None = None) -> dict[str, Any]:
    print("==> Enumerating session PDFs (I-XXX)...", file=sys.stderr)
    sessions = discover_session_pdfs(cache_dir=cache_dir)
    print(f"==> Znaleziono {len(sessions)} sesji VII kadencji", file=sys.stderr)

    if limit_sessions:
        sessions = sessions[:limit_sessions]

    out_sessions = []
    all_councilors: set[str] = set()
    total_votes = 0

    for sess in sessions:
        print(f"\n=> Sesja {sess['session_number']} ({sess['session_arabic']}): "
              f"{sess['pdf_url'][:80]}", file=sys.stderr)

        tmp_pdf = (cache_dir or Path("/tmp")) / f"_lub_{sess['session_number']}.pdf"
        tmp_pdf.parent.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(sess["pdf_data"])

        try:
            parsed = parse_voting_pdf(tmp_pdf)
            ok, fail, _errors = validate_parsed(parsed)
            print(f"   votes={parsed['vote_count']}, walidacja={ok}/{parsed['vote_count']}",
                  file=sys.stderr)
            for v in parsed["votes"]:
                for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                    for name in v.get("named_votes", {}).get(cat, []):
                        all_councilors.add(name)
            total_votes += parsed["vote_count"]
            out_sessions.append({
                "session_number": parsed.get("number_roman") or sess["session_number"],
                "session_arabic": sess["session_arabic"],
                "date": parsed["date"],
                "votes": parsed["votes"],
                "vote_count": parsed["vote_count"],
                "source_url": sess["pdf_url"],
            })
        except Exception as e:
            print(f"  WARN: parse {sess['session_number']}: {e}", file=sys.stderr)
        finally:
            if not cache_dir:
                tmp_pdf.unlink(missing_ok=True)

    return {
        "kadencja": KADENCJA_ID,
        "kadencja_label": KADENCJA_LABEL,
        "councilors": sorted(all_councilors),
        "total_councilors": len(all_councilors),
        "sessions": out_sessions,
        "total_sessions": len(out_sessions),
        "total_votes": total_votes,
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{BASE} (URL pattern enumeration I-{MAX_SESSION_NUMBER})",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Sejmik Województwa Lubelskiego")
    parser.add_argument("--cache", type=Path, default=Path(".cache/lubelskie"))
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("docs/kadencja-2024-2029.json"))
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
