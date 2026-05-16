#!/usr/bin/env python3
"""
Radoskop Kielce — BIP scraper.

Źródło: bipum.kielce.eu/rada-miasta-kielce/sesje-rady-miasta-kielce/kadencjaixlata20182024/

Uwaga URL: BIP Kielce użył nazwy ścieżki 'kadencjaixlata20182024' DLA IX kadencji
2024-2029 (typo w nazwie path), więc trzymamy się tego co działa.

Struktura:
  Index kadencji: /sesje-rady-miasta-kielce/kadencjaixlata20182024/
  Page 'YYYY-rok/' per rok: lista sesji z linkami do per-session page.
  Per sesja: 'Raport z głosowań' jako PDF attachment plus protokół plus
  porządek obrad plus nagranie.

Vote PDF format (BIP Kielce):
  N. Głosowanie w sprawie {opis}... - czas głosowania: DATA, godz. HH:MM,
     wyniki: ZA: X, PRZECIW: Y, WSTRZYMUJĘ SIĘ: Z, BRAK GŁOSU: W, NIEOBECNI: V
  Wyniki imienne: Imię Nazwisko (GŁOS), Imię Nazwisko (GŁOS), ...

Format imiennych głosów to comma-separated lista "Imię Nazwisko (GŁOS)",
gdzie GŁOS to ZA/PRZECIW/WSTRZYMUJĘ SIĘ/BRAK GŁOSU/NIEOBECNY.

UWAGA: kielce.esesja.pl była używana w przeszłości ale obecnie ma martwy
listing (zero sesji widocznych). Kielce przeniosły publikację głosowań na BIP.
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


BASE_URL = "https://bipum.kielce.eu"
KADENCJA_PATH = "/rada-miasta-kielce/sesje-rady-miasta-kielce/kadencjaixlata20182024"

POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

# Pattern dla tytułu sesji: "Sesja Rady Miasta Kielce w dniu DD miesiąc YYYY roku"
# Wariant: "Nadzwyczajna/Uroczysta sesja ..."
SESSION_DATE_RE = re.compile(
    r"w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.UNICODE | re.IGNORECASE,
)

# Vote block w PDF zaczyna się od "{N}. Głosowanie w sprawie ..."
VOTE_BLOCK_RE = re.compile(
    r"^(\d+)\.\s+(.+?)(?=^\d+\.\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Pattern dla wyniku imiennego: "Imię Nazwisko (GŁOS)"
# Imię + Nazwisko - polskie znaki dozwolone, podwójne nazwiska z myślnikiem.
NAMED_VOTE_RE = re.compile(
    r"([A-ZŻŹĆŁŚŃĘĄÓ][a-zżźćłśńęąó]+(?:[-\s]+[A-ZŻŹĆŁŚŃĘĄÓ][a-zżźćłśńęąó]+)+)\s*\(([^)]+)\)"
)

# Mapowanie etykiety głosu → kategoria.
VOTE_LABELS = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIE": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu",
    "NIE GŁOSOWAŁ": "brak_glosu",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
}


def parse_session_title(title: str) -> tuple[str, str] | None:
    """Parse 'Sesja Rady Miasta Kielce w dniu 28 sierpnia 2025 roku'
    → ('', '2025-08-28'). Numer sesji nie jest w tytule (jest w protokole),
    więc fallback do daty jako number.
    """
    m = SESSION_DATE_RE.search(title)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = POLISH_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return "", f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None


def _norm_vote_kw(kw: str) -> str:
    """Mapuje keyword głosu (case-insensitive) na kategorię."""
    upper = kw.upper().strip()
    return VOTE_LABELS.get(upper, "brak_glosu")


class KielceScraper(BipScraper):
    """Scraper BIP Kielce dla kadencji IX 2024-2029."""

    YEARS_TO_SCAN = (2024, 2025, 2026, 2027, 2028, 2029)

    def discover_sessions(self) -> list[dict]:
        """Iteruje yearly index pages żeby zebrać wszystkie sesje."""
        sessions: list[dict] = []
        seen: set[str] = set()
        for year in self.YEARS_TO_SCAN:
            year_url = f"{self.base_url}{KADENCJA_PATH}/{year}-rok/"
            try:
                soup = self.fetch(year_url)
            except Exception as exc:
                # Strona dla danego roku może nie istnieć (przyszłe lata)
                print(f"  Year {year} not available: {exc}", file=sys.stderr)
                continue
            for a in soup.find_all("a", href=True):
                href = a.get("href") or ""
                if not isinstance(href, str):
                    continue
                if "sesja" not in href.lower() or f"{year}-rok/" not in href:
                    continue
                # Wyklucz parent year-index links
                if href.endswith(f"{year}-rok/") or href.endswith(f"{year}-rok.html"):
                    continue
                full_url = urljoin(BASE_URL, href).split("#")[0]
                if full_url in seen:
                    continue
                title = a.get_text(strip=True)
                parsed = parse_session_title(title)
                if not parsed:
                    continue
                _, date = parsed
                seen.add(full_url)
                sessions.append({
                    "url": full_url,
                    "date": date,
                    "number": "",  # nie ma w tytule, zostaje data jako fallback
                    "title": title,
                })
        sessions.sort(key=lambda s: s["date"])
        return sessions

    def parse_session_votes(self, session: dict) -> list[dict]:
        """Z strony sesji wyciąga 'Raport z głosowań' PDF i parsuje go."""
        try:
            soup = self.fetch(session["url"])
        except Exception as exc:
            print(f"    Session page fetch failed: {exc}", file=sys.stderr)
            return []

        # Znajdź link do "Raport z głosowań" (PDF)
        raport_url = None
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True).lower()
            if "raport" in text and "głosowa" in text:
                href = a["href"]
                raport_url = urljoin(BASE_URL, href)
                break
        if not raport_url:
            return []

        return self._parse_raport_pdf(raport_url, session)

    def _parse_raport_pdf(self, pdf_url: str, session: dict) -> list[dict]:
        if pdfplumber is None:
            return []
        try:
            data = self.fetch_bytes(pdf_url)
        except Exception as exc:
            print(f"    PDF fetch failed: {exc}", file=sys.stderr)
            return []
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
            return []

        # Wykryj sekcję "Przeprowadzone głosowania" i parsuj od niej
        marker = text.find("Przeprowadzone głosowania")
        if marker != -1:
            body = text[marker:]
        else:
            body = text

        votes: list[dict] = []
        for m in VOTE_BLOCK_RE.finditer(body):
            num = int(m.group(1))
            block = m.group(0)
            vote = self._parse_vote_block(block, num, session, pdf_url)
            if vote:
                votes.append(vote)
        return votes

    def _parse_vote_block(self, block: str, num: int, session: dict, pdf_url: str) -> dict | None:
        """Parsuj jeden blok głosowania z PDF Kielce.

        Format:
            N. Głosowanie w sprawie {topic}... - czas głosowania: ...,
               wyniki: ZA: X, PRZECIW: Y, ...
            Wyniki imienne: Name1 (VOTE1), Name2 (VOTE2), ...
        """
        # Topic: tekst między '\d+. ' a ' - czas głosowania:'
        topic_match = re.search(
            r"^\d+\.\s+(.+?)\s+-\s+czas\s+głosowania",
            block, re.DOTALL | re.IGNORECASE,
        )
        if not topic_match:
            return None
        topic = re.sub(r"\s+", " ", topic_match.group(1)).strip()

        # Druk: regex 'projekt nr X uchwały' albo 'druk nr X'
        druk = None
        druk_m = re.search(r"(?:druku?|projekt(?:u)?)\s+nr\s+(\d+)", topic, re.IGNORECASE)
        if druk_m:
            druk = druk_m.group(1)

        # Wyniki imienne — wszystko po "Wyniki imienne:" do końca bloku
        imienne_match = re.search(r"Wyniki\s+imienne\s*:\s*(.+)", block, re.DOTALL | re.IGNORECASE)
        named: dict[str, list[str]] = {
            "za": [], "przeciw": [], "wstrzymal_sie": [],
            "brak_glosu": [], "nieobecni": [],
        }
        if imienne_match:
            imienne_text = imienne_match.group(1)
            # Match wszystkie "Imię Nazwisko (GŁOS)"
            for vm in NAMED_VOTE_RE.finditer(imienne_text):
                name = re.sub(r"\s+", " ", vm.group(1)).strip()
                vote_kw = vm.group(2).strip()
                cat = _norm_vote_kw(vote_kw)
                # Map to canonical name from config if possible
                canonical = self._resolve_canonical_name(name)
                if canonical:
                    named[cat].append(canonical)
                else:
                    # Append raw name as fallback (lib_bip_static.resolve_club
                    # tries last-name match anyway)
                    named[cat].append(name)

        vote_id = f"{session['date']}_{num:03d}_000"
        return {
            "id": vote_id,
            "topic": topic[:500],
            "druk": druk,
            "source_url": session["url"],
            "pdf_url": pdf_url,
            "named_votes": named,
        }

    def _resolve_canonical_name(self, raw: str) -> str | None:
        """Mapuje 'Imię Nazwisko' z PDF na canonical name z config (jeśli istnieje)."""
        if not self.councilors:
            return raw  # brak config, zwracaj jak jest
        # Bezpośredni match
        if raw in self.councilors:
            return raw
        # Case-insensitive match
        for canonical in self.councilors.keys():
            if canonical.lower() == raw.lower():
                return canonical
        # Last-name match (Imię z PDF może różnić się formą)
        parts = raw.split()
        if parts:
            last = parts[-1].lower()
            for canonical in self.councilors.keys():
                cparts = canonical.split()
                if cparts and cparts[-1].lower() == last:
                    return canonical
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
    parser = argparse.ArgumentParser(description="Radoskop Kielce (BIP)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = HERE.parent.parent / "config.json"
    councilors = load_councilors(config_path)

    scraper = KielceScraper(
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
