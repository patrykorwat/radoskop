#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Kujawsko-Pomorskiego, kadencja 2024-2029.

BIP kuj-pom (bip.kujawsko-pomorskie.pl) publikuje "Wydruki głosowań VII kadencji"
jako skanowane PDF, 1 strona = 1 głosowanie. Każda sesja ma osobną podstronę
zawierającą 1 PDF z wszystkimi głosowaniami.

Struktura:
  Indeks /13254/1098/wydruki-glosowan-sejmiku-vii-kadencji.html
    Linki: /{ID}/1098/wydruki-glosowan-{rzymski}-sesji-sejmiku-DDMMYYYY-r.html
      Każda podstrona ma link do PDF: /download/attachment/{N}/wydruki-glosowan-z-{roman}-sesji-sejmiku-DDMMYY.pdf

Format PDF: skanowany, 1 strona/głosowanie, tabela Lp/Nazwisko/Decyzja.
Wymaga OCR. **Polish pack `tesseract-ocr-pol` wysoce zalecany** dla
poprawnego rozpoznawania imion radnych z diakrytykami. Bez polish pack
accuracy ~25%, z polish pack ~90%.

Używamy `lib_voting_pdf_table.parse_voting_pdf_per_page` które
automatycznie wykrywa skan i odpala OCR.

Output: kadencja-2024-2029.json zgodne ze schemą innych sejmików.
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


SCRIPT_DIR = Path(__file__).resolve().parent
RADOSKOP_SCRIPTS = SCRIPT_DIR.parent.parent.parent / "scripts"
if str(RADOSKOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(RADOSKOP_SCRIPTS))

from lib_voting_pdf_table import parse_voting_pdf_per_page, validate_parsed  # noqa: E402


BASE = "https://bip.kujawsko-pomorskie.pl"
INDEX_URL = f"{BASE}/13254/1098/wydruki-glosowan-sejmiku-vii-kadencji.html"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 60
SLEEP_BETWEEN = 0.1


def fetch(url: str, *, cache_dir: Path | None = None, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
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


def fetch_html(url: str, *, cache_dir: Path | None = None) -> str:
    return fetch(url, cache_dir=cache_dir, suffix=".html").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_session_pdfs(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Zwraca listę sesji + URL do PDF.

    Each: {"session_number" (rzymski), "subpage_url", "pdf_url"}
    """
    body = fetch_html(INDEX_URL, cache_dir=cache_dir)

    # Linki podstron: "Wydruki głosowań XXIV sesji Sejmiku - 20.04.2026 r."
    pattern = re.compile(
        r'href="([^"]*?/wydruki-glosowan-([ivxlcdm]+)-sesji-sejmiku-([\d]{6,8})[^"]*?)"',
        re.IGNORECASE,
    )
    seen = set()
    sessions = []
    for href, roman_lower, _date_str in pattern.findall(body):
        if href in seen:
            continue
        seen.add(href)
        subpage_url = href if href.startswith("http") else BASE + href

        try:
            subpage_body = fetch_html(subpage_url, cache_dir=cache_dir)
        except Exception as e:
            print(f"  WARN: subpage {href}: {e}", file=sys.stderr)
            continue
        # PDF link w subpage: /download/attachment/N/wydruki-glosowan-z-roman-sesji-...
        pdf_match = re.search(
            r'href="([^"]*?/download/attachment/[^"]+\.pdf[^"]*?)"',
            subpage_body,
        )
        if not pdf_match:
            print(f"  WARN: brak PDF w {href}", file=sys.stderr)
            continue
        pdf_url = pdf_match.group(1)
        if not pdf_url.startswith("http"):
            pdf_url = BASE + pdf_url

        sessions.append({
            "session_number": roman_lower.upper(),
            "subpage_url": subpage_url,
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
        print(f"\n=> Sesja {sess['session_number']}", file=sys.stderr)

        try:
            pdf_data = fetch(sess["pdf_url"], cache_dir=cache_dir, suffix=".pdf")
        except Exception as e:
            print(f"  WARN: download {sess['session_number']}: {e}", file=sys.stderr)
            continue

        tmp_pdf = (cache_dir or Path("/tmp")) / f"_kp_{sess['session_number']}.pdf"
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
        "ocr_warning": (
            "Skanowane PDF, accuracy zależy od polish tesseract pack. "
            "Zainstaluj `tesseract-ocr-pol` (apt) na NAS dla ~90% accuracy."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Sejmik Województwa Kujawsko-Pomorskiego")
    parser.add_argument("--cache", type=Path, default=Path(".cache/kujawsko-pomorskie"))
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
