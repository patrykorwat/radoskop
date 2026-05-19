#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Lubuskiego, kadencja 2024-2029.

BIP lubuski (bip.lubuskie.pl) publikuje imienne wykazy głosowań VII kadencji
pod URL `/958/VII_kadencja__282024-2029_29/`. Każda sesja ma osobny PDF
skanowany ~18MB. Format jest podobny do kujawsko-pomorskiego: 1 strona
PDF = 1 głosowanie z tabelą Lp/Nazwisko/Decyzja.

Struktura indeksu:
  href=".../system/pobierz.php?plik={N}_sesja_Sejmiku_DD.MM.YYYY_-_Imienny_wykaz_glosowan_radnych.pdf&id={hash}"

Wymaga OCR z polish pack. Używamy `parse_voting_pdf_per_page`.

UWAGA wydajności: PDFy są ~18MB, każda sesja ma 100-200 stron skanu.
OCR jednej sesji to ~10-15 minut. 20 sesji = 3-5 godzin pełnego scrape.
Cache jest tutaj kluczowy.

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

from lib_voting_pdf_table import parse_voting_pdf_per_page, validate_parsed  # noqa: E402


BASE = "https://bip.lubuskie.pl"
INDEX_URL = f"{BASE}/958/VII_kadencja__282024-2029_29/"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 120  # Większy timeout bo PDFy są duże
SLEEP_BETWEEN = 0.1

# Sslcontext z bypass na wypadek problemów z cert (precaution)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch(url: str, *, cache_dir: Path | None = None, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
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
# Discovery
# ---------------------------------------------------------------------------


def discover_session_pdfs(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Zwraca listę sesji z URL do PDF + datą + numerem rzymskim.

    Each: {"session_number", "date_iso", "pdf_url"}
    """
    body = fetch_html(INDEX_URL, cache_dir=cache_dir)

    # Pattern URL: ...plik={N}_sesja_Sejmiku_DD.MM.YYYY_-_Imienny_wykaz_glosowan_radnych.pdf
    # gdzie {N} to rzymski albo zniekształcone unicode
    pattern = re.compile(
        r'href="([^"]*?pobierz\.php\?plik=([IVXLCDM]+)_sesja_Sejmiku_'
        r'(\d{2})\.(\d{2})\.(\d{4})[^"]*?Imienny_wykaz_glosowan[^"]*?)"',
        re.IGNORECASE,
    )
    sessions = []
    seen = set()
    for href, roman, dd, mm, yyyy in pattern.findall(body):
        if href in seen:
            continue
        seen.add(href)
        pdf_url = href if href.startswith("http") else BASE + href
        # HTML encoded &amp; → &
        pdf_url = pdf_url.replace("&amp;", "&")
        sessions.append({
            "session_number": roman.upper(),
            "date_iso": f"{yyyy}-{int(mm):02d}-{int(dd):02d}",
            "pdf_url": pdf_url,
        })
    return sessions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_kadencja(cache_dir: Path | None = None,
                   limit_sessions: int | None = None) -> dict[str, Any]:
    print("==> Discovering session PDFs...", file=sys.stderr)
    sessions = discover_session_pdfs(cache_dir=cache_dir)
    print(f"==> Found {len(sessions)} sesji VII kadencji", file=sys.stderr)

    if limit_sessions:
        sessions = sessions[:limit_sessions]

    out_sessions = []
    all_councilors: set[str] = set()
    total_votes = 0

    for sess in sessions:
        print(f"\n=> Sesja {sess['session_number']} ({sess['date_iso']})", file=sys.stderr)

        try:
            pdf_data = fetch(sess["pdf_url"], cache_dir=cache_dir, suffix=".pdf")
        except Exception as e:
            print(f"  WARN: download {sess['session_number']}: {e}", file=sys.stderr)
            continue

        tmp_pdf = (cache_dir or Path("/tmp")) / f"_lubu_{sess['session_number']}.pdf"
        tmp_pdf.parent.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(pdf_data)

        try:
            parsed = parse_voting_pdf_per_page(tmp_pdf)
            ok, fail, _ = validate_parsed(parsed)
            print(f"   votes={parsed['vote_count']}, walidacja={ok}/{parsed['vote_count']}",
                  file=sys.stderr)
            for v in parsed["votes"]:
                for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                    for name in v.get("named_votes", {}).get(cat, []):
                        all_councilors.add(name)
            total_votes += parsed["vote_count"]
            out_sessions.append({
                "session_number": parsed.get("number_roman") or sess["session_number"],
                "date": sess["date_iso"],
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
        "ocr_warning": (
            "Skanowane PDF ~18MB/sesja. Wymaga `tesseract-ocr-pol` na NAS. "
            "Pełny scrape 20 sesji = ~3-5h, cache absolutnie konieczny."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Sejmik Województwa Lubuskiego")
    parser.add_argument("--cache", type=Path, default=Path(".cache/lubuskie"))
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("docs/kadencja-2024-2029.json"))
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit sesji (debug)")
    args = parser.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    kadencja = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit)

    # Guard: jeśli OCR/parse zwrócił 0 sesji (typowo brak tesseract-ocr-pol
    # albo pdf2image w image), nie nadpisuj istniejącego pliku zerowymi
    # danymi. Lepiej zostawić stary dobry kadencja-2024-2029.json i pozwolić
    # downstream'om (build_assembly_metrics, deploy) działać dalej na nim.
    if kadencja["total_sessions"] == 0 and args.output.exists():
        print(f"\n✗ Zero sesji — pomijam zapis {args.output} (zostaje poprzednia wersja)", file=sys.stderr)
        print("  Sprawdź czy NAS ma tesseract-ocr-pol + pdf2image + pytesseract.", file=sys.stderr)
        return 1

    with args.output.open("w", encoding="utf-8") as f:
        json.dump(kadencja, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved {args.output}", file=sys.stderr)
    print(f"  Sesji: {kadencja['total_sessions']}", file=sys.stderr)
    print(f"  Głosowań: {kadencja['total_votes']}", file=sys.stderr)
    print(f"  Radnych: {kadencja['total_councilors']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
