#!/usr/bin/env python3
"""
Radoskop Rzeszów — scraper sesji Rady Miasta Rzeszowa.

Źródło danych: bip.erzeszow.pl (CMS Pro3W).

Struktura:
  Index sesji: https://bip.erzeszow.pl/3635-sesje-rady-miasta-rzeszowa-uchwaly-protokoly-nagrania.html
  Lista 40+ sesji z kadencji IX (2024-2029). Każda sesja ma osobną stronę
  z linkami do: Nagrania, Protokołu, Uchwał. Brak osobnej strony "Wyniki
  głosowań" — głosowania imienne nie są publikowane jako oddzielne PDF/HTML,
  są zwykle wbudowane w pełny protokół sesji.

Status:
  - Skład rady (25 radnych) plus przypisania do klubów: kompletne
    w config.json (PiS 9, KO 7, Rozwój Rzeszowa 5, Razem dla Rzeszowa 4)
  - Discovery sesji: implementuje, parsuje listę z indeksu
  - Roll-call votes: NIE publikowane przez BIP w formie strukturalnej.
    Pełne protokoły sesji to wielostronicowe PDFy które wymagałyby OCR
    i parsowania pojedynczo per uchwała. Pomijamy do momentu zmiany formatu
    w BIP lub udostępnienia API w otwartedane.erzeszow.pl.

  Scraper emituje listę sesji bez vote data — miasto pojawia się
  w Radoskopie z pełnym składem rady i listą posiedzeń, ale bez frekwencji
  i zgodności (te wymagają roll-call). Frontend pokazuje placeholdery.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from lib_bip_static import BipScraper  # noqa: E402


BASE_URL = "https://bip.erzeszow.pl"
INDEX_URL = (
    f"{BASE_URL}/3635-sesje-rady-miasta-rzeszowa-uchwaly-protokoly-nagrania.html"
)

POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

SESSION_TITLE_RE = re.compile(
    r"([IVXLC]+)\s+Sesja\s+-\s+(\d{1,2})\s+(\S+)\s+(\d{4})",
    re.UNICODE | re.IGNORECASE,
)


def parse_session_title(title: str) -> tuple[str, str] | None:
    """Parse 'XXIII Sesja - 25 marca 2025 r.' → ('XXIII', '2025-03-25').

    BIP submenu prefixes link text z '» ' (right-pointing chevron), więc
    używamy `search` zamiast `match` żeby nie wymagać startu linii.
    """
    m = SESSION_TITLE_RE.search(title.strip())
    if not m:
        return None
    roman, day, month_name, year = m.groups()
    month = POLISH_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        date = f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None
    return roman.upper(), date


class RzeszowScraper(BipScraper):
    """Scraper BIP Rzeszów dla kadencji IX 2024-2029.

    BIP nie publikuje głosowań imiennych w strukturze nadającej się do
    automatycznego parsowania (osobne PDFy per głosowanie albo HTML tabela).
    Roll-call wbudowane w pełne protokoły PDF kilkudziesięciostronicowe.
    Aktualnie scraper wyciąga tylko listę sesji.
    """

    def discover_sessions(self) -> list[dict]:
        """Parse session list from INDEX_URL.

        Sesje renderowane jako linki w treści strony plus w submenu. Filtrujemy
        po wzorcu URL (sesja prefix) i wyciągamy datę z tytułu.
        """
        soup = self.fetch(INDEX_URL)
        sessions: list[dict] = []
        seen: set[str] = set()
        # Linki do per-session pages mają wzorzec
        # /3635-sesje.../{id}-{roman}-sesja-...-r.html
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not href or not isinstance(href, str):
                continue
            if "-sesja-" not in href:
                continue
            # Wyklucz sub-strony (nagranie/protokol/uchwaly). Parent path
            # zawiera już '-uchwaly-protokoly-nagrania' więc substring match
            # by łapał wszystko. Sprawdzamy że slug pliku (po ostatnim /)
            # nie kończy się na te suffixy.
            slug = href.rsplit("/", 1)[-1].split("#")[0].split("?")[0]
            slug_base = slug.removesuffix(".html")
            if any(slug_base.endswith(sfx) for sfx in ("nagranie", "protokol", "protokoly", "uchwaly")):
                continue
            url = urljoin(BASE_URL, href)
            # Strip fragment+query żeby dedup pomijał #tresc anchor (te same
            # sesje są w głównej liście i w submenu).
            base_url = url.split("#")[0].split("?")[0]
            if base_url in seen:
                continue
            url = base_url
            title = a.get_text(strip=True)
            parsed = parse_session_title(title)
            if not parsed:
                continue
            roman, date = parsed
            seen.add(base_url)
            sessions.append({
                "url": url,
                "date": date,
                "number": roman,
                "title": title,
            })
        sessions.sort(key=lambda s: s["date"])
        return sessions

    def parse_session_votes(self, session: dict) -> list[dict]:
        """Roll-call votes nie są publikowane przez BIP Rzeszów.

        Patrz docstring klasy. Funkcja zwraca pustą listę żeby pipeline
        widział sesję ale bez głosowań. Po zmianie formatu w BIP (np. jeśli
        udostępnią osobne PDFy per uchwała) ten parser można dopisać.
        """
        return []

    def build_councilors(self, all_votes, sessions, existing_profiles):
        """Override: seedujemy radnych z config.json nawet bez głosowań.

        Domyślny `BipScraper.build_councilors` agreguje statystyki z
        `all_votes`. Dla Rzeszowa głosowania nie są publikowane jako
        struktura per-uchwała, więc `all_votes` jest puste i bez tego
        override frontend dostaje pustą listę councilors → strona pokazuje
        "Ładowanie danych..." bez końca.

        Tu wstrzykujemy wszystkich radnych z `self.councilors` (config) jako
        seed, potem normalna agregacja po votes (jeśli kiedyś będą) dodaje
        statystyki. Klub wyciągamy z `self.club_lookup` (build_name_lookup
        z config).
        """
        result = super().build_councilors(all_votes, sessions, existing_profiles)
        present = {c["name"] for c in result}
        for name in self.councilors.keys():
            if name in present:
                continue
            result.append({
                "name": name,
                "club": self.resolve_club(name),
                "district": None,
                "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
                "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
                "rebellion_count": 0, "rebellions": [],
                "has_voting_data": False, "has_activity_data": False,
            })
        # Posortuj po nazwisku żeby zachować deterministyczny output
        result.sort(key=lambda c: c["name"])
        return result


def load_councilors(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Radoskop Rzeszów")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = HERE.parent.parent / "config.json"
    councilors = load_councilors(config_path)

    scraper = RzeszowScraper(
        base_url=BASE_URL,
        kadencje=KADENCJE,
        councilors=councilors,
        delay=1.0,
        cache_dir=args.cache_dir,
        default_kadencja="2024-2029",
    )
    return scraper.run(
        output_path=args.output,
        profiles_path=args.profiles,
        max_sessions=args.max_sessions or 0,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
