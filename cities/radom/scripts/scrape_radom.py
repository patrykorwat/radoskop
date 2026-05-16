#!/usr/bin/env python3
"""
Radoskop Radom — BIP scraper.

Źródło danych: bip.radom.pl/ra/rada-miejska/sesje/protokoly-i-glosowania/

Struktura:
  Lista sesji (paginowana): /protokoly-i-glosowania?page=N
    Każdy wpis: "Protokół i głosowania z {ROMAN} sesji ... w dniu DD miesiąc YYYY r."
  Per sesja: strona z listą plików PDF do pobrania.
    - "Protokół nr {ROMAN}/{YEAR}" - pełen protokół sesji
    - "Głosowanie nr N" - per-vote PDF z imienną tabelą głosów
  Vote PDF (~167KB każdy): header z tematem + tabela 25 radnych w 2 kolumnach.

Format Vote PDF:
  Lp. Nazwisko i imię Głos Lp. Nazwisko i imię Głos
  1.  Adam Bocheński  NIEOBECNY  14. Łukasz Podlewski NIEOBECNY
  ...
  13. Katarzyna Pastuszka-Chrobotowicz WSTRZYMUJĘ SIĘ

Council members + club assignments z config.json (club_assignments).

UWAGA: radom.esesja.pl była używana w przeszłości ale od kadencji IX
(2024-) Radom przeniósł publikację głosowań na BIP. eSesja Radom
ma martwy listing (ostatnia widoczna sesja z 2014).
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

try:
    import pdfplumber  # type: ignore
except ImportError:
    pdfplumber = None


BASE_URL = "https://bip.radom.pl"
INDEX_PATH = "/ra/rada-miejska/sesje/protokoly-i-glosowania"
SESSION_LINK_PATTERN = re.compile(r"/ra/rada-miejska/sesje/protokoly-i-glosowania/\d+,")

POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

SESSION_DATE_RE = re.compile(
    r"z\s+([IVXLCDM]+)\s+(?:nadzwyczajnej\s+|uroczystej\s+|absolutoryjnej\s+)?sesji"
    r".*?(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.UNICODE | re.IGNORECASE,
)


VOTE_KEYWORDS = {
    "za": ["ZA"],
    "przeciw": ["PRZECIW"],
    "wstrzymal_sie": ["WSTRZYMUJĘ SIĘ", "WSTRZYMAŁ SIĘ", "WSTRZYMUJE SIE"],
    "brak_glosu": ["NIE GŁOSOWAŁ", "NIE GŁOSOWAŁA", "BRAK GŁOSU"],
    "nieobecni": ["NIEOBECNY", "NIEOBECNA"],
}

# Linia z radnym ma postać:
#   "1. Imię Nazwisko ZA" lub
#   "1. Imię Nazwisko ZA 14. Imię Nazwisko PRZECIW"
# Numer + kropka, potem imię i nazwisko (może mieć spacje, polskie znaki,
# dwuczłonowe), na końcu keyword głosu.
# Sortujemy keywordy malejąco po długości żeby WSTRZYMUJĘ SIĘ matchowało
# przed ZA.
_VOTE_KW_ALT = "|".join(
    sorted(
        (re.escape(kw) for kws in VOTE_KEYWORDS.values() for kw in kws),
        key=len,
        reverse=True,
    )
)
RADNY_ROW_RE = re.compile(
    r"(\d{1,2})\.\s+([^\d]+?)\s+(" + _VOTE_KW_ALT + r")",
    re.UNICODE,
)


def parse_session_title(title: str) -> tuple[str, str] | None:
    """Parse 'Protokół i głosowania z XXV sesji ... w dniu 26 maja 2025 r.'
    → ('XXV', '2025-05-26'). Returns None on failure."""
    m = SESSION_DATE_RE.search(title)
    if not m:
        return None
    roman, day, month_name, year = m.groups()
    month = POLISH_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return roman.upper(), f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None


def _norm_vote_keyword(kw: str) -> str:
    """Map keyword string → category code (za/przeciw/wstrzymal_sie/...)."""
    upper = kw.upper()
    for cat, keywords in VOTE_KEYWORDS.items():
        if upper in keywords:
            return cat
    return "brak_glosu"


class RadomScraper(BipScraper):
    """Scraper BIP Radom dla kadencji IX 2024-2029."""

    INDEX_URL = BASE_URL + INDEX_PATH
    MAX_PAGES = 20  # safety cap, realnie 4-5 stron na kadencję

    def discover_sessions(self) -> list[dict]:
        """Parse session list across paginated /protokoly-i-glosowania pages."""
        sessions: list[dict] = []
        seen: set[str] = set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.INDEX_URL if page == 1 else f"{self.INDEX_URL}?page={page}"
            try:
                soup = self.fetch(url)
            except Exception as e:
                print(f"  Failed to fetch {url}: {e}", file=sys.stderr)
                break

            found_on_page = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not SESSION_LINK_PATTERN.search(href):
                    continue
                full_url = urljoin(BASE_URL, href).split("#")[0]
                if full_url in seen:
                    continue
                title = a.get_text(strip=True)
                parsed = parse_session_title(title)
                if not parsed:
                    continue
                roman, date = parsed
                seen.add(full_url)
                sessions.append({
                    "url": full_url,
                    "date": date,
                    "number": roman,
                    "title": title,
                })
                found_on_page += 1
            if found_on_page == 0:
                break
        sessions.sort(key=lambda s: s["date"])
        return sessions

    def parse_session_votes(self, session: dict) -> list[dict]:
        """Wyciąga listę głosowań z BIP session page.

        Każde głosowanie to osobny PDF link "Głosowanie nr N pdf, ...".
        Pobieramy każdy PDF i parsujemy roll-call.
        """
        try:
            soup = self.fetch(session["url"])
        except Exception as exc:
            print(f"    Session page fetch failed: {exc}", file=sys.stderr)
            return []

        votes: list[dict] = []
        # Linki vote PDF: anchor tekst zaczyna się od "Głosowanie nr N",
        # href ma format /download/{x}/{y}/Sesja{N}Glosowanie{M}Data{...}.pdf
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/download/" not in href or not href.lower().endswith(".pdf"):
                continue
            label = a.get_text(strip=True)
            m = re.match(r"\s*Głosowanie\s+nr\s+(\d+)", label, re.IGNORECASE)
            if not m:
                continue
            vote_num = int(m.group(1))
            pdf_url = urljoin(BASE_URL, href)
            parsed = self._parse_vote_pdf(pdf_url)
            named = parsed.get("named_votes", {})
            vote_id = f"{session['date']}_{vote_num:03d}_000"
            votes.append({
                "id": vote_id,
                "topic": (parsed.get("topic") or f"Głosowanie {vote_num}")[:500],
                "druk": parsed.get("druk"),
                "source_url": session["url"],
                "pdf_url": pdf_url,
                "named_votes": named,
            })
        return votes

    def _parse_vote_pdf(self, pdf_url: str) -> dict:
        """Pobiera i parsuje pojedynczy vote PDF.

        Zwraca {"topic": str, "druk": str|None, "named_votes": dict}.
        """
        empty = {"topic": "", "druk": None, "named_votes": {
            "za": [], "przeciw": [], "wstrzymal_sie": [],
            "brak_glosu": [], "nieobecni": [],
        }}
        if pdfplumber is None:
            return empty
        try:
            data = self.fetch_bytes(pdf_url)
        except Exception as exc:
            print(f"    PDF fetch failed: {exc}", file=sys.stderr)
            return empty
        try:
            import io
            text_parts: list[str] = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    text_parts.append(t)
            text = "\n".join(text_parts)
        except Exception as exc:
            print(f"    PDF parse failed: {exc}", file=sys.stderr)
            return empty

        named: dict[str, list[str]] = {
            "za": [], "przeciw": [], "wstrzymal_sie": [],
            "brak_glosu": [], "nieobecni": [],
        }

        # Match wszystkie "{lp}. {nazwisko} {GŁOS}" wystąpienia w tekście.
        # Format radomski ma 2 kolumny per linia, więc finditer zwraca
        # 2 matche per linia. Wyciągamy znaną listę radnych z config.
        known_names: set[str] = set()
        if self.councilors:
            known_names = {n.upper() for n in self.councilors.keys()}

        for m in RADNY_ROW_RE.finditer(text):
            name_raw = m.group(2).strip()
            keyword = m.group(3)
            # Czyść nadmiarowe spacje
            name_clean = re.sub(r"\s+", " ", name_raw)
            cat = _norm_vote_keyword(keyword)
            # Spróbuj zmapować do canonical z config (Imię Nazwisko)
            canonical = self._resolve_canonical_name(name_clean, known_names)
            if canonical:
                named[cat].append(canonical)

        # Wyciągnij topic z headera: linia po "Głosowanie" przed "Typ głosowania"
        topic = ""
        druk = None
        m_topic = re.search(r"\.\s+(.+?)\s+Typ\s+głosowania", text, re.DOTALL | re.IGNORECASE)
        if m_topic:
            topic = re.sub(r"\s+", " ", m_topic.group(1)).strip()
        m_druk = re.search(r"druku\s+nr\s+(\d+)", text, re.IGNORECASE)
        if m_druk:
            druk = m_druk.group(1)

        return {"topic": topic, "druk": druk, "named_votes": named}

    @staticmethod
    def _resolve_canonical_name(raw: str, known_upper: set[str]) -> str | None:
        """Mapuje 'Imię Nazwisko' z PDF (mixed case) na canonical z config.

        BIP Radom używa formatu 'Imię Nazwisko' z prawidłowymi znakami
        polskimi, więc match jest case-insensitive. Plus fallback po
        nazwiskach (czasem PDF ma 'Imię Drugie Nazwisko').
        """
        upper = raw.upper()
        if upper in known_upper:
            # Znajdź canonical (z prawidłowym case) — przebieg po set,
            # ale na ogół 25 elementów, więc tanie.
            for k in known_upper:
                if k == upper:
                    return raw  # raw już jest poprawny
        # Try matching by last name (last word).
        last = upper.split()[-1] if upper.split() else ""
        if last:
            for k in known_upper:
                if k.split()[-1] == last:
                    return raw
        return None


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
    parser = argparse.ArgumentParser(description="Radoskop Radom (BIP)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = HERE.parent.parent / "config.json"
    councilors = load_councilors(config_path)

    scraper = RadomScraper(
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
