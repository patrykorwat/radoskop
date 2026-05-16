#!/usr/bin/env python3
"""
Radoskop Bielsko-Biała — scraper sesji Rady Miejskiej.

Źródło danych: rm.bielsko-biala.pl (Drupal portal Biura Rady, nie eSesja).

Struktura:
  1. Lista sesji:    https://rm.bielsko-biala.pl/protokoly/2024-2029
     HTML z linkami do per-session pages. Daty w polskim formacie ("27 marca 2025 r.").
  2. Per session:    /protokoly/2024-2029/{slug}
     Tabela z punktami porządku obrad. W kolumnie "Wynik głosowania"
     są odnośniki "Wynik głosowania N" prowadzące do PDF na SharePoint
     (sesjerm-my.sharepoint.com). Każdy PDF = jedno głosowanie imienne
     z listą radnych i ich głosami.
  3. PDF głosowania: SharePoint share link. Wymaga `download=1` parametru
     do bezpośredniego pobrania pliku. Format PDF: tabela radnych
     z kolumnami za/przeciw/wstrzymał się/nieobecny.

Parser PDF jest best-effort i bazuje na fuzzy matchingu nazwisk radnych
z config.club_assignments + keyword detection ("ZA", "PRZECIW", "WSTRZYMAŁ
SIĘ", "NIEOBECNY"). Format SharePoint może się różnić między sesjami i
parser próbuje kilku strategii. Jeśli żadna nie zadziała, zwraca pustą
listę named_votes (vote count zachowany z summary headera jeśli się da
wyciągnąć).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from lib_bip_static import BipScraper  # noqa: E402

try:
    import pdfplumber  # type: ignore
except ImportError:
    pdfplumber = None


BASE_URL = "https://rm.bielsko-biala.pl"
INDEX_PATH = "/protokoly/2024-2029"

POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

ROMAN_RE = re.compile(r"^(?P<roman>[IVXLC]+)\s+sesja", re.IGNORECASE)
SESSION_DATE_RE = re.compile(r"(\d{1,2})\s+(\w+)\s+(\d{4})", re.UNICODE)
SHAREPOINT_HOST = "sesjerm-my.sharepoint.com"

VOTE_CATEGORIES = {
    "za": ["ZA"],
    "przeciw": ["PRZECIW"],
    "wstrzymal_sie": ["WSTRZYMAŁ SIĘ", "WSTRZYMAL SIE", "WSTRZYMAŁA SIĘ", "WSTRZYMAŁA"],
    "brak_glosu": ["BRAK GŁOSU", "BRAK GLOSU", "NIE GŁOSOWAŁ", "NIE GŁOSOWAŁA"],
    "nieobecni": ["NIEOBECNY", "NIEOBECNA"],
}


def parse_polish_date(s: str) -> str | None:
    """Parse 'DD miesiąc YYYY' → 'YYYY-MM-DD'. Returns None on failure."""
    m = SESSION_DATE_RE.search(s)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = POLISH_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None


def extract_session_number(title: str) -> str:
    """Extract 'XV' from 'XV sesja Rady Miejskiej...'."""
    m = ROMAN_RE.match(title.strip())
    return m.group("roman").upper() if m else ""


def force_sharepoint_download(url: str) -> str:
    """Append download=1 to SharePoint share URL so we get the file bytes.

    SharePoint anonymous share links render an in-browser viewer by default.
    Adding ?download=1 (lub &download=1 jeśli już są query params) zwraca
    surowy bajt PDF. Jeśli URL nie jest SharePoint, zwraca bez zmian.
    """
    p = urlparse(url)
    if SHAREPOINT_HOST not in p.netloc.lower():
        return url
    q = parse_qs(p.query, keep_blank_values=True)
    q["download"] = ["1"]
    new_query = urlencode(q, doseq=True)
    return urlunparse(p._replace(query=new_query))


class BielskoBialaScraper(BipScraper):
    """Drupal/SharePoint scraper dla Rady Miejskiej w Bielsku-Białej."""

    INDEX_URL = BASE_URL + INDEX_PATH

    def discover_sessions(self) -> list[dict]:
        """Zwraca [{date, number, url, title}] z indeksu protokołów IX kadencji."""
        soup = self.fetch(self.INDEX_URL)
        sessions: list[dict] = []
        seen: set[str] = set()
        for a in soup.select(f'a[href*="{INDEX_PATH}/"]'):
            href = a.get("href") or ""
            if not href or not isinstance(href, str):
                continue
            # Tylko linki na konkretne sesje, nie sam indeks.
            if href.rstrip("/").endswith("/2024-2029"):
                continue
            url = urljoin(BASE_URL, href)
            if url in seen:
                continue
            seen.add(url)
            title = a.get_text(strip=True)
            date = parse_polish_date(title)
            if not date:
                continue
            sessions.append({
                "url": url,
                "date": date,
                "number": extract_session_number(title),
                "title": title,
            })
        # Sortuj rosnąco po dacie — pipeline preferuje chronologiczne dane.
        sessions.sort(key=lambda s: s["date"])
        return sessions

    def parse_session_votes(self, session: dict) -> list[dict]:
        soup = self.fetch(session["url"])
        votes: list[dict] = []
        # Każdy wiersz tabeli porządku obrad zawiera linki "Wynik głosowania N"
        # do SharePoint PDFs. Wyciągamy je wraz z kontekstem (tytuł punktu,
        # numer druku) z tej samej komórki/wiersza.
        for row in soup.select("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            topic_cell = cells[1] if len(cells) > 1 else None
            results_cell = None
            for c in cells:
                if "głosowania" in c.get_text().lower() or "głosowanie" in c.get_text().lower():
                    if c.find("a", href=lambda h: h and SHAREPOINT_HOST in (h or "")):
                        results_cell = c
                        break
            if not results_cell:
                continue
            topic = topic_cell.get_text(" ", strip=True) if topic_cell else ""
            druk = self._extract_druk_number(topic)
            for vote_link in results_cell.find_all("a", href=True):
                href = vote_link["href"]
                if SHAREPOINT_HOST not in href:
                    continue
                label = vote_link.get_text(strip=True)
                pdf_url = force_sharepoint_download(href)
                named_votes = self._parse_vote_pdf(pdf_url)
                vote_id = self._make_vote_id(session, label, pdf_url)
                votes.append({
                    "id": vote_id,
                    "topic": (topic or label)[:500],
                    "druk": druk,
                    "source_url": session["url"],
                    "pdf_url": pdf_url,
                    "named_votes": named_votes,
                })
        return votes

    @staticmethod
    def _extract_druk_number(topic: str) -> str | None:
        m = re.search(r"DRUK\s+NR\s+(\d+)", topic, re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _make_vote_id(session: dict, label: str, pdf_url: str) -> str:
        m = re.search(r"(\d+)", label)
        num = m.group(1) if m else "0"
        return f"{session['date']}_{int(num):03d}_000"

    def _parse_vote_pdf(self, pdf_url: str) -> dict:
        """Wyciąga listę głosów imiennych z PDF SharePoint.

        Format PDF (Rada Miejska Bielska-Białej):
          Header z tytułem głosowania (DRUK NR X, wynik za/przeciw/...).
          Tabela radnych, każdy wiersz w jednej linii tekstu:
            "Lp Karta IMIĘ NAZWISKO Funkcja GŁOS"
          gdzie Funkcja to "Radny/Radna Rady Miejskiej", "Wiceprzewodniczący
          Rady Miejskiej", "Przewodnicząca Rady Miejskiej" (czasem zawija
          na kolejną linię, ale słowo głosu jest na końcu pierwszej).
          GŁOS to: ZA, PRZECIW, WSTRZYMAŁ SIĘ, NIEOBECNY, NIE GŁOSOWAŁ.

        Parser:
          1. Pobierz PDF (z cache), wyciągnij tekst pdfplumber'em.
          2. Iteruj linie. Dla każdej linii zaczynającej się od dwóch liczb
             (lp + karta), znajdź IMIĘ NAZWISKO matching councilor list
             i kategorię głosu na końcu linii.

        Jeśli pdfplumber nie zainstalowany albo PDF się nie pobierze,
        zwraca pusty dict.
        """
        if pdfplumber is None:
            return {}
        try:
            data = self.fetch_bytes(pdf_url)
        except Exception as exc:
            print(f"    PDF fetch failed: {exc}", file=sys.stderr)
            return {}
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
            return {}

        named: dict[str, list[str]] = {
            "za": [], "przeciw": [], "wstrzymal_sie": [],
            "brak_glosu": [], "nieobecni": [],
        }
        if not self.councilors:
            return named

        # Lookup: UPPER(IMIĘ NAZWISKO) → canonical name (jak w config.json).
        name_index: dict[str, str] = {}
        for canonical in self.councilors.keys():
            name_index[canonical.upper()] = canonical
            parts = canonical.split()
            if len(parts) >= 2:
                first, last = parts[0].upper(), parts[-1].upper()
                name_index[f"{first} {last}"] = canonical

        # Pattern dla linii: dwie liczby na początku, potem dowolny tekst,
        # na końcu jedno z kategorii głosu. Greedy match z kategorią głosu
        # zachłannie chwyta najdłuższy keyword (WSTRZYMAŁ SIĘ przed ZA).
        # Sortujemy kategorie po długości malejąco żeby dłuższe miały
        # pierwszeństwo (WSTRZYMAŁ SIĘ przed ZA).
        sorted_categories = sorted(
            ((cat, kw) for cat, kws in VOTE_CATEGORIES.items() for kw in kws),
            key=lambda x: -len(x[1]),
        )

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Wymagamy żeby linia zaczynała się od dwóch liczb (lp i karta).
            if not re.match(r"^\d+\s+\d+\s+", line):
                continue
            line_upper = line.upper()
            # Wykryj kategorię głosu z końca linii.
            cat_found: str | None = None
            for cat, kw in sorted_categories:
                if line_upper.endswith(kw):
                    cat_found = cat
                    break
            if cat_found is None:
                continue
            # Wykryj nazwisko: weź segment między prefixem liczb a funkcją.
            # Funkcje rozpoznajemy po keywordach.
            func_keywords = [
                "RADNY RADY MIEJSKIEJ",
                "RADNA RADY MIEJSKIEJ",
                "WICEPRZEWODNICZĄCY RADY",
                "WICEPRZEWODNICZĄCA RADY",
                "PRZEWODNICZĄCY RADY",
                "PRZEWODNICZĄCA RADY",
            ]
            name_segment = None
            for fk in func_keywords:
                idx = line_upper.find(fk)
                if idx > 0:
                    name_segment = line_upper[:idx].strip()
                    break
            if not name_segment:
                continue
            # Strip leading "Lp Karta" prefix.
            m = re.match(r"^\d+\s+\d+\s+(.+)$", name_segment)
            if not m:
                continue
            candidate = m.group(1).strip()
            canonical = name_index.get(candidate)
            if not canonical:
                # Fallback: try matching by last name + first name in any order.
                for full, cn in name_index.items():
                    if all(part in candidate for part in full.split()):
                        canonical = cn
                        break
            if not canonical:
                continue
            named[cat_found].append(canonical)
        return named


def load_councilors(config_path: Path) -> dict:
    """Czyta club_assignments z config.json miasta."""
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
    parser = argparse.ArgumentParser(description="Radoskop Bielsko-Biała")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = HERE.parent.parent / "config.json"
    councilors = load_councilors(config_path)

    scraper = BielskoBialaScraper(
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
