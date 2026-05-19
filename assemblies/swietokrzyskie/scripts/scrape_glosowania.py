#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Świętokrzyskiego, kadencja 2024-2029.

BIP świętokrzyski (bip.sejmik.kielce.pl) ma **broken SSL cert**: standardowa
weryfikacja zwraca `CERTIFICATE_VERIFY_FAILED`, dlatego cały HTTP traffic
wymaga `ssl.CERT_NONE`. Bez bypass scraper widzi "0B body" i myli z timeoutem.

Struktura BIP:
  Indeks /1269-imienne-wykazy-glosowan-radnych-sejmiku-wojewodztwa-swietokrzyskiego-...
    Linki: /1269-.../{NNN}-imienny-wykaz-glosowan-radnych-na-{rzymski}-sesji-...
      Każda podstrona zawiera 1 link do PDF:
      /download/112584-wyniki-glosowan/.../{slug}.html
      (rozszerzenie .html ale Content-Type to application/pdf)

PDF format: eSesja standard (potwierdzone XXVII sesja 11/11 walidacja OK).
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


BASE = "https://bip.sejmik.kielce.pl"
INDEX_URL = (
    f"{BASE}/1269-imienne-wykazy-glosowan-radnych-sejmiku-wojewodztwa-"
    f"swietokrzyskiego-vii-kadencji-lata-2024-2029.html"
)
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05

# SSL context z disabled verify (broken cert na bip.sejmik.kielce.pl)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# HTTP + cache (SSL bypass)
# ---------------------------------------------------------------------------


def fetch(url: str, *, cache_dir: Path | None = None, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT, context=SSL_CTX) as resp:
            data = resp.read()
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GET {url} failed: {e}") from e
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    time.sleep(SLEEP_BETWEEN)
    return data


def fetch_html(url: str, *, cache_dir: Path | None = None) -> str:
    return fetch(url, cache_dir=cache_dir, suffix=".html").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Discovery: lista podstron sesji + per podstrona link do PDF
# ---------------------------------------------------------------------------


def discover_session_pdfs(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Zwraca listę sesji + URL do PDF imiennego wykazu.

    Each: {"session_number": "XXVII", "subpage_url", "pdf_url", "date"}
    """
    body = fetch_html(INDEX_URL, cache_dir=cache_dir)

    # Linki do podstron: href="/1269-.../{NNN}-imienny-wykaz-glosowan-radnych-na-{roman}-sesji-..."
    pattern = (
        r'href="(/1269-[^"]*?'
        r'(\d+)-imienny-wykaz-glosowan-radnych-na-([ivxlcdm]+)-sesji[^"]*?)"'
    )
    matches = re.findall(pattern, body, re.IGNORECASE)
    print(f"==> Sesji na indeksie: {len(matches)}", file=sys.stderr)

    sessions = []
    seen = set()
    for href, _doc_id, roman_lower in matches:
        if href in seen:
            continue
        seen.add(href)
        subpage_url = BASE + href
        try:
            subpage_body = fetch_html(subpage_url, cache_dir=cache_dir)
        except Exception as e:
            print(f"  WARN: subpage {href}: {e}", file=sys.stderr)
            continue
        # Link do PDF: /download/112584-wyniki-glosowan/...
        pdf_match = re.search(
            r'href="(/download/\d+-wyniki-glosowan/[^"]+)"',
            subpage_body,
        )
        if not pdf_match:
            print(f"  WARN: brak PDF w podstronie {href}", file=sys.stderr)
            continue
        pdf_url = BASE + pdf_match.group(1)

        # Wyciągnij datę dodania (typowy format "DD miesiąca YYYY" w treści)
        sessions.append({
            "session_number": roman_lower.upper(),
            "subpage_url": subpage_url,
            "pdf_url": pdf_url,
            "date": None,  # zostanie wypełniona przez parse_voting_pdf z PDF
        })
    return sessions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_kadencja(cache_dir: Path | None = None,
                   limit_sessions: int | None = None) -> dict[str, Any]:
    sessions = discover_session_pdfs(cache_dir=cache_dir)
    if limit_sessions:
        sessions = sessions[:limit_sessions]

    out_sessions = []
    all_councilors: set[str] = set()
    total_votes = 0

    for sess in sessions:
        print(f"\n=> Sesja {sess['session_number']}: {sess['pdf_url'][:80]}",
              file=sys.stderr)

        try:
            pdf_data = fetch(sess["pdf_url"], cache_dir=cache_dir, suffix=".pdf")
        except Exception as e:
            print(f"  WARN: PDF {sess['session_number']}: {e}", file=sys.stderr)
            continue

        # Zapisz tymczasowo i parsuj
        tmp_pdf = (cache_dir or Path("/tmp")) / f"_swiet_{sess['session_number']}.pdf"
        tmp_pdf.parent.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(pdf_data)

        try:
            parsed = parse_voting_pdf(tmp_pdf)
            ok, fail, errors = validate_parsed(parsed)
            print(f"   votes={parsed['vote_count']}, walidacja={ok}/{parsed['vote_count']}",
                  file=sys.stderr)
            for v in parsed["votes"]:
                for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                    for name in v.get("named_votes", {}).get(cat, []):
                        all_councilors.add(name)
            total_votes += parsed["vote_count"]
            out_sessions.append({
                "session_number": parsed.get("number_roman") or sess["session_number"],
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
        "source": INDEX_URL,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Sejmik Województwa Świętokrzyskiego")
    parser.add_argument("--cache", type=Path, default=Path(".cache/swietokrzyskie"))
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
