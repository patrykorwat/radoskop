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
import difflib
import json
import re
import sys
import time
import unicodedata
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


# ---------------------------------------------------------------------------
# Roster + korekta nazwisk z OCR (wzorem lubuskiego)
# ---------------------------------------------------------------------------
# OCR skanów daje dziesiątki wariantów tego samego nazwiska (literówki, urwane
# znaki, brak diakrytyków, odwrócona kolejność imię/nazwisko), przez co surowa
# lista "radnych" puchła do setek. Dopasowujemy każde nazwisko z OCR do
# OFICJALNEGO składu Sejmiku VII kadencji (BIP, /13158/), a warianty nie
# pasujące do nikogo odrzucamy. Źródło 1:1 z BIP, nie z agregatorów.
#
# 30 radnych obecnych + 2 z wygaszonym mandatem w trakcie kadencji (głosowali
# na wczesnych sesjach), żeby ich głosy też trafiły do właściwej osoby.
SEED_ROSTER = [
    "Piotr Całbecki", "Michał Czepek", "Jacek Gajewski", "Marek Gralik",
    "Wojciech Jaranowski", "Aneta Jędrzejewska", "Marcel Kałużny",
    "Jarosław Katulski", "Radosław Kempinski", "Sławomir Kopyść",
    "Ewa Kozanecka", "Katarzyna Stranz-Kaja", "Dariusz Kurzawa",
    "Katarzyna Lubańska", "Józef Łyczak", "Anna Maćkowska", "Robert Malinowski",
    "Anna Niewiadomska", "Zbigniew Ostrowski", "Elżbieta Piniewska",
    "Leszek Pluciński", "Tadeusz Pogoda", "Przemysław Przybylski",
    "Józef Ramlau", "Wojciech Szczęsny", "Przemysław Sznajdrowski",
    "Jarosław Wenderlich", "Marek Witkowski", "Paweł Zgórzyński",
    "Przemysław Ziemecki",
    # mandat wygaszony w trakcie kadencji:
    "Łukasz Krupa", "Jacek Woźny",
]


def _name_key(name: str) -> str:
    """Klucz dopasowania: bez diakrytyków, małymi literami, tokeny ≥2 znaki
    posortowane (odporne na kolejność imię/nazwisko i na śmieci OCR).

    Uwaga: 'ł'/'Ł' NIE rozkłada się przez NFKD (to osobna litera, nie litera
    bazowa + znak diakrytyczny), więc mapujemy je ręcznie na 'l' — inaczej
    re.split tnie 'Całbecki' na 'ca'/'becki' i dopasowanie pada."""
    s = name.lower().replace("ł", "l")
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if len(t) > 1]
    return " ".join(sorted(toks))


_ROSTER_BY_KEY = {_name_key(n): n for n in SEED_ROSTER}
_ROSTER_KEYS = list(_ROSTER_BY_KEY.keys())
_MATCH_CACHE: dict[str, str | None] = {}


def _canonical_name(raw: str) -> str | None:
    """Mapuje nazwisko z OCR na oficjalne z rostera (fuzzy); None = śmieć OCR."""
    key = _name_key(raw)
    if not key:
        return None
    if key in _MATCH_CACHE:
        return _MATCH_CACHE[key]
    canon = _ROSTER_BY_KEY.get(key)
    if canon is None:
        m = difflib.get_close_matches(key, _ROSTER_KEYS, n=1, cutoff=0.82)
        canon = _ROSTER_BY_KEY[m[0]] if m else None
    _MATCH_CACHE[key] = canon
    return canon


def correct_named_votes(named: dict[str, list[str]]) -> dict[str, list[str]]:
    """Zamienia surowe nazwiska OCR na kanoniczne z rostera; odrzuca
    niedopasowane i deduplikuje w obrębie kategorii."""
    out = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
    for cat in out:
        seen = set()
        for raw in named.get(cat, []):
            canon = _canonical_name(raw)
            if canon and canon not in seen:
                seen.add(canon)
                out[cat].append(canon)
    return out


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
                   limit_sessions: int | None = None,
                   output_path: Path | None = None) -> dict[str, Any]:
    print("==> Discovering session PDFs...", file=sys.stderr)
    sessions = discover_session_pdfs(cache_dir=cache_dir)
    print(f"==> Found {len(sessions)} sesji VII kadencji", file=sys.stderr)

    if limit_sessions:
        sessions = sessions[:limit_sessions]

    out_sessions = []
    all_councilors: set[str] = set()
    total_votes = 0

    def _assemble() -> dict[str, Any]:
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

    def _flush() -> None:
        # Zapis przyrostowy po KAŻDEJ sesji: ubicie procesu na timeoucie nie
        # traci już zparsowanych sesji, a jedna wolna sesja (gruby skan OCR) nie
        # blokuje publikacji pozostałych. Atomowo: temp + replace.
        if output_path is None or not out_sessions:
            return
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_text(json.dumps(_assemble(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(output_path)

    for sess in sessions:
        print(f"\n=> Sesja {sess['session_number']}", file=sys.stderr)

        try:
            pdf_data = fetch(sess["pdf_url"], cache_dir=cache_dir, suffix=".pdf")
        except Exception as e:
            print(f"  WARN: download {sess['session_number']}: {e}", file=sys.stderr)
            continue

        # Cache parsowania per sesja, kluczowany TREŚCIĄ PDF (jak cache OCR), żeby
        # ukończone sesje wczytywały się natychmiast i cały budżet kolejnego runu
        # szedł na front (nieukończoną, grubą sesję) zamiast na re-parsowanie.
        sess_cache = None
        if cache_dir:
            sess_cache = cache_dir / (md5(pdf_data).hexdigest() + ".session.json")
        parsed_session = None
        if sess_cache and sess_cache.is_file():
            try:
                parsed_session = json.loads(sess_cache.read_text(encoding="utf-8"))
                print(f"   (cache) votes={parsed_session['vote_count']}", file=sys.stderr)
            except Exception:
                parsed_session = None

        if parsed_session is None:
            tmp_pdf = (cache_dir or Path("/tmp")) / f"_kp_{sess['session_number']}.pdf"
            tmp_pdf.parent.mkdir(parents=True, exist_ok=True)
            tmp_pdf.write_bytes(pdf_data)
            try:
                parsed = parse_voting_pdf_per_page(tmp_pdf)
                ok, fail, _ = validate_parsed(parsed)
                print(f"   votes={parsed['vote_count']}, walidacja={ok}/{parsed['vote_count']}",
                      file=sys.stderr)
                parsed_session = {
                    "session_number": parsed.get("number_roman") or sess["session_number"],
                    "date": parsed["date"],
                    "votes": parsed["votes"],
                    "vote_count": parsed["vote_count"],
                    "source_url": sess["pdf_url"],
                }
                if sess_cache:
                    sess_cache.write_text(
                        json.dumps(parsed_session, ensure_ascii=False),
                        encoding="utf-8")
            except Exception as e:
                print(f"  WARN: parse {sess['session_number']}: {e}", file=sys.stderr)
                continue
            finally:
                if not cache_dir:
                    tmp_pdf.unlink(missing_ok=True)

        # Korekta nazwisk z OCR do oficjalnego rostera. Robimy to po wczytaniu
        # (cache trzyma surowe nazwiska, więc poprawka roster-a działa też dla
        # sesji zcache'owanych i da się ją w przyszłości ulepszyć bez kasowania
        # cache).
        for v in parsed_session["votes"]:
            v["named_votes"] = correct_named_votes(v.get("named_votes") or {})

        for v in parsed_session["votes"]:
            for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                for name in v["named_votes"][cat]:
                    all_councilors.add(name)
        total_votes += parsed_session["vote_count"]
        out_sessions.append(parsed_session)
        _flush()

    _flush()
    return _assemble()


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

    kadencja = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit,
                              output_path=args.output)

    # Guard: jeśli OCR/parse zwrócił 0 sesji (typowo brak tesseract-ocr-pol
    # albo pdf2image w image), nie nadpisuj istniejącego pliku zerowymi
    # danymi. Lepiej zostawić stary dobry kadencja-2024-2029.json.
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
