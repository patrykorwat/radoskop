#!/usr/bin/env python3
"""
Radoskop Gdańsk — dynamiczne pobieranie PDF-ów "Głosowania" z BIP.

Zastępuje ręcznie utrzymywaną listę wget (download_glosowania.sh w katalogu
scratch), która zatrzymywała się na ostatniej dopisanej sesji. Strony BIP
"Protokoly z sesji z {ROK} r. audio/wideo" (te same, z których scrape_protokoly
bierze protokoły) linkują też PDF "Głosowania" per sesja na download.cloudgdansk.pl.
Tu odkrywamy je dynamicznie dla wszystkich lat z BIP_LISTING_PAGES i pobieramy
do katalogu pdfs/, który parsuje parse_pdf.py.

Idempotentny: pomija pliki już pobrane. Skanowane PDF-y (stare sesje) i tak
odrzuci parse_pdf.py.

Użycie:
    python3 download_glosowania.py --out <scratch>/pdfs [--year 2026] [--debug]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
from scrape_protokoly import BIP_LISTING_PAGES  # noqa: E402

HEADERS = {"User-Agent": "Radoskop/1.0 (https://gdansk.radoskop.pl; kontakt@radoskop.pl)"}
CLOUD_RE = re.compile(r"download\.cloudgdansk\.pl/.+/d/(\d+)/", re.IGNORECASE)


def discover_glosowania(year: int, url: str, debug: bool = False) -> dict[str, str]:
    """Zwraca {id_cloudgdansk: url_pdf} dla linków 'Głosowania' na stronie roku."""
    out: dict[str, str] = {}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [{year}] BŁĄD strony listy: {exc}")
        return out
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if "download.cloudgdansk.pl" not in href or not href.lower().endswith(".pdf"):
            continue
        # PDF głosowań ma w nazwie linku/URL "głosowania"/"glosowania"
        # (a NIE "protokół"); chcemy tylko wyniki głosowań.
        blob = f"{text} {href}".lower()
        if "głosowani" not in blob and "glosowani" not in blob:
            continue
        m = CLOUD_RE.search(href)
        if not m:
            continue
        out[m.group(1)] = href
    if debug:
        print(f"  [{year}] znaleziono {len(out)} PDF głosowań")
    return out


def _already_have(out_dir: Path, cid: str) -> bool:
    """Czy sesja o tym ID cloudgdansk jest już w katalogu pod DOWOLNĄ nazwą.

    Statyczna download_glosowania.sh zapisuje pliki z opisowym sufiksem
    (np. '2020_202005148656_glosowania-30-04.pdf'), więc sprawdzamy obecność ID
    w nazwie — inaczej dołożylibyśmy duplikat tej samej sesji pod inną nazwą
    (podwójne liczenie głosów). Dzięki temu downloader jest czysto addytywny:
    dokłada tylko NOWE sesje, nie dubluje tego, co już pobrała statyczna lista.
    """
    return any(cid in p.name for p in out_dir.glob("*.pdf"))


def download(out_dir: Path, only_year: int | None = None, debug: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = {y: u for y, u in BIP_LISTING_PAGES.items() if only_year is None or y == only_year}
    found = downloaded = skipped = 0
    for year in sorted(pages, reverse=True):
        url = pages[year]
        print(f"[{year}] {url}")
        for cid, pdf_url in discover_glosowania(year, url, debug=debug).items():
            found += 1
            dest = out_dir / f"{year}_{cid}_glosowania.pdf"
            if _already_have(out_dir, cid):
                skipped += 1
                continue
            try:
                r = requests.get(pdf_url, headers=HEADERS, timeout=60)
                r.raise_for_status()
                if b"%PDF" not in r.content[:1024] and len(r.content) < 5000:
                    print(f"    UWAGA: nie-PDF {pdf_url}")
                    continue
                dest.write_bytes(r.content)
                downloaded += 1
                print(f"    + {dest.name} ({len(r.content)//1024} KB)")
                time.sleep(0.3)
            except Exception as exc:
                print(f"    BŁĄD {pdf_url}: {exc}")
    print(f"\nZnaleziono {found}, pobrano {downloaded}, pominięto (cache) {skipped}.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Gdańsk — dynamiczne pobieranie głosowań z BIP")
    ap.add_argument("--out", required=True, type=Path, help="Katalog docelowy pdfs/")
    ap.add_argument("--year", type=int, default=None, help="Tylko jeden rok (debug)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    download(args.out, only_year=args.year, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
