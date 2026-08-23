#!/usr/bin/env python3
"""
lib_bip_net.py — adapter dla BIP-ów na platformie Nefeni (bip.net.pl, Next.js).

Niektóre samorządy (np. Piechowice) nie publikują głosowań imiennych na
publicznej liście eSesja — zamiast tego wyniki głosowań wydają jako PDF-y
"Raport z przeprowadzonych głosowań" (generowane przez app.esesja.pl) na
swoim BIPie na platformie Nefeni. Ten moduł dostarcza:

  1. listę artykułów (sesji) w danej kategorii BIP, z paginacją `?page=N`,
  2. URL załącznika PDF (raportu głosowań) z strony artykułu,
  3. podstawowe metadane sesji z tytułu artykułu (numer rzymski + data).

Struktura Nefeni:
  - strona kategorii:      {base}/kategorie/{catId}-{catSlug}?lang=PL[&page=N]
  - strona artykułu:       {base}/kategorie/{catId}-{catSlug}/artykuly/{artId}-{artSlug}?lang=PL
  - URL załącznika PDF:    https://{apiHost}/api/attachments/{id}
                           (wpisany w SSR payload strony artykułu)

Treść artykułu ładuje się z SSR — nie ma wygodnego endpointu JSON do zgadywania,
więc parsujemy stabilny HTML (tak jak klasyczne scrapery BIP w tym repo).

Parsowanie samych raportów PDF (format eSesja standard, "Wyniki imienne") robi
istniejący lib_voting_pdf_table.parse_voting_pdf — ten moduł NIE dubluje parsera.

Usage (w scraperze per-miasto):
    from lib_bip_net import NefeniRaport

    nb = NefeniRaport(base_url="https://piechowice.bip.net.pl", debug=True)
    articles = nb.articles_in_category("/kategorie/295-ix-kadencja-...")
    for a in articles:
        pdf_url, filename = nb.attachment_for_article(a.url)
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4 lxml", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Metadane sesji (wyciągane z tytułu artykułu raportu)
# ---------------------------------------------------------------------------

POLISH_MONTHS_GEN = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
}

# "Raport z przeprowadzonych głosowań podczas XXVII absolutoryjnej sesji Rady
# Miasta Piechowice w dniu 9 lipca 2026 roku"
ROMAN_RE = re.compile(r"podczas\s+([IVXLCDM]+)\s+(?:nadzwyczajnej\s+)?", re.IGNORECASE)
DATE_GEN_RE = re.compile(
    r"w\s+dniu\s+(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|"
    r"sierpnia|września|października|listopada|grudnia)\s+(\d{4})",
    re.IGNORECASE,
)


@dataclass
class SessionMeta:
    """Meta sesji wyciągnięte z artykułu raportu (numer, data, URL, PDF)."""

    number: str            # rzymski, np. "XXVII"
    date: str              # ISO YYYY-MM-DD
    title: str
    article_url: str
    article_id: str
    published: str = ""
    pdf_url: str = ""
    pdf_filename: str = ""


def parse_session_from_title(title: str) -> tuple[str, str]:
    """Zwraca (numer_rzymski, data_iso) z tytułu artykułu raportu.

    Np. '... podczas XXVI absolutoryjnej sesji ... w dniu 11 czerwca 2026 roku'
    -> ('XXVI', '2026-06-11'). Zwraca ('', '') gdy nie parsowalne.
    """
    rm = ROMAN_RE.search(title)
    numeral = rm.group(1).upper() if rm else ""
    dm = DATE_GEN_RE.search(title)
    iso = ""
    if dm:
        iso = f"{dm.group(3)}-{POLISH_MONTHS_GEN[dm.group(2).lower()]}-{int(dm.group(1)):02d}"
    return numeral, iso


# ---------------------------------------------------------------------------
# Główna klasa adaptera
# ---------------------------------------------------------------------------

ATTACHMENT_URL_RE = re.compile(
    r"https?://([a-z0-9.-]*?)\.bip\.net\.pl/api/attachments/(\d+)"
)
ATTACHMENT_URL_ANY = re.compile(
    r"https?://([^\"'\\ ]+?)/api/attachments/(\d+)"
)


class NefeniRaport:
    """Reusable listowanie artykułów raportów + ekstrakcja PDF na bip.net.pl."""

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        base_url: str,
        *,
        delay: float = 0.3,
        timeout: int = 40,
        debug: bool = False,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self.debug = debug
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.UA, "Accept-Language": "pl-PL,pl;q=0.9"})
        self._cache = Path(cache_dir) if cache_dir else None
        if self._cache:
            self._cache.mkdir(parents=True, exist_ok=True)

    # -- HTTP ----------------------------------------------------------------

    def _cache_path(self, url: str) -> Optional[Path]:
        if self._cache is None:
            return None
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return self._cache / f"{h}.html"

    def fetch(self, url: str, use_cache: bool = True) -> str:
        """Pobiera HTML strony Nefeni z opcjonalnym cachem dyskowym."""
        cp = self._cache_path(url) if use_cache else None
        if cp and cp.exists() and cp.stat().st_size > 100:
            return cp.read_text(encoding="utf-8")
        if self.debug:
            print(f"  GET {url}")
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.text
        if cp:
            try:
                cp.write_text(text, encoding="utf-8")
            except OSError:
                pass
        time.sleep(self.delay)
        return text

    # -- Artykuły kategorii --------------------------------------------------

    def articles_in_category(self, category_path: str, *,
                             require="raport", max_pages: int = 50) -> list[SessionMeta]:
        """Listuje artykuły w kategorii (paginacja `?page=N`), URL-e artykułów.

        Zwraca SessionMeta z wypełnionym number/date (z tytułu) i article_url.
        `require` — podciąg tytułu wymagany do wzięcia artykułu ('' = wszystkie);
        filtr bezpieczeństwa, bo kategoria raportów może mieć też inne wpisy.
        """
        cat = category_path.strip("/")
        articles: list[SessionMeta] = []
        seen: set[str] = set()
        page = 1
        while page <= max_pages:
            url = self.base_url + f"/{cat}?lang=PL"
            if page > 1:
                url += f"&page={page}"
            try:
                html = self.fetch(url, use_cache=False)
            except Exception as exc:
                if self.debug:
                    print(f"  [warn] strona {page} kategorii: {exc}")
                break
            soup = BeautifulSoup(html, "lxml")
            before = len(articles)
            # Artykuły: linki /{cat}/artykuly/{id}-{slug}. `cat` to ścieżka
            # "kategorie/295-...", a href ma wiodący slash: "/kategorie/295-...".
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.match(rf"/{re.escape(cat)}/artykuly/(\d+)-([^?]+)", href)
                if not m:
                    continue
                title = a.get_text(" ", strip=True).strip()
                if require and require.lower() not in title.lower():
                    continue
                art_id, slug = m.group(1), m.group(2).rstrip("?")
                full = href if href.startswith("http") else self.base_url + href
                norm = full.split("?")[0]
                if norm in seen:
                    continue
                seen.add(norm)
                numeral, iso = parse_session_from_title(title)
                articles.append(SessionMeta(
                    number=numeral, date=iso, title=title,
                    article_url=full, article_id=art_id,
                ))
            added = len(articles) - before
            if self.debug:
                print(f"  Kategoria strona {page}: +{added} artykułów (razem {len(articles)})")
            # Nefeni nie renderuje linku "następna" w SSR — paginacja działa po
            # `?page=N` (sprawdzone: page=1 -> 10 ar., page=2 -> 6 ar.). Koniec
            # = strona bez nowych artykułów (albo mniej niż pełna strona).
            if added == 0:
                break
            page += 1
        return articles

    # -- Załącznik PDF artykułu ----------------------------------------------

    def attachment_for_article(self, article_url: str) -> tuple[str, str]:
        """Zwraca (pdf_url, pdf_filename) raportu z strony artykułu.

        URL załącznika ('https://{api}/api/attachments/{id}') siedzi w SSR
        payload (Next.js __NEXT_DATA__ / props). Przy braku — ('', '').
        """
        html = self.fetch(article_url, use_cache=True)
        pdf_url = ""
        pdf_name = ""
        # Preferowany i jednoznaczny: docelowy host {miasteczko}-api.bip.net.pl
        m = ATTACHMENT_URL_RE.search(html)
        if m:
            pdf_url = f"https://{m.group(1)}.bip.net.pl/api/attachments/{m.group(2)}"
        else:
            m = ATTACHMENT_URL_ANY.search(html)
            if m:
                pdf_url = f"https://{m.group(1)}/api/attachments/{m.group(2)}"
        # Nazwa pliku PDF (np. 'Raport ... .pdf') — bierzemy z props/tekstu.
        fn = re.search(r"([^\"'\\]*\.pdf)", html, re.IGNORECASE)
        if fn:
            pdf_name = fn.group(1).strip()
        return pdf_url, pdf_name


def download_pdf(http_session: requests.Session, url: str, pdf_dir: Path,
                 headers: dict | None = None, timeout: int = 60) -> Optional[Path]:
    """Pobiera PDF do cache (sha256 po URL), zwraca ścieżkę albo None.

    Osobna funkcja, żeby scraper mógł dzielić jeden requests.Session (z
    NefeniRaport._session, który ma odpowiednie UA) z pobieraniem załączników.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    out = pdf_dir / f"{key}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        resp = http_session.get(url, headers=headers or {}, timeout=timeout, stream=True)
        resp.raise_for_status()
        with out.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as exc:
        print(f"      BŁĄD pobierania PDF {url}: {exc}")
        if out.exists():
            out.unlink(missing_ok=True)
        return None
