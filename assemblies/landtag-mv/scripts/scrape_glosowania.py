#!/usr/bin/env python3
"""Scraper namentliche Abstimmungen Landtag Mecklenburg-Vorpommern.

Źródło danych: landtag-mv.de. Landtag MV publikuje per Plenarsitzung
pojedyncze pliki Abstimmungsprotokoll z systemu Votebox jako PDF tekstowy.
PDFy są linkowane z miesięcznych stron archiwum pod
`/plenum-und-ausschuesse/plenum/vergangene-plenarsitzungen/{miesiac-rok}/`
(np. `oktober-2025`, `juli-2025`).

Każdy PDF Votebox to single-page protokół jednej namentliche Abstimmung,
z header (data, czas, drucksache, wynik), wynikami summary (Ja/Nein/
Enthaltung/Nicht abgestimmt) i sekcjami imiennymi posłów. Każdy poseł
zapisany jako `(Fraktion) Nachname, Vorname`, kolejne pozycje rozdzielone
średnikami.

Lista posłów 8. WP: landtag-mv.de/abgeordnete-und-fraktionen/abgeordnete.

Output: kadencja-2021-2026.json zgodne ze schemą innych sejmików Radoskop.

Wymaga: requests, beautifulsoup4, pdfplumber.

UWAGA: skrypt fetchuje strony archiwum miesięcznego strict text/html,
więc landtag-mv.de musi zwracać pełen HTML bez JS. Probe agent
potwierdził że tak (statyczne TYPO3-rendered).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from hashlib import md5
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
import pdfplumber


BASE = "https://www.landtag-mv.de"
ARCHIVE_BASE = f"{BASE}/plenum-und-ausschuesse/plenum/vergangene-plenarsitzungen/"
ABGEORDNETE_URL = f"{BASE}/abgeordnete-und-fraktionen/abgeordnete"
KADENCJA_ID = "2021-2026"
KADENCJA_LABEL = "8. Wahlperiode (2021–2026)"
KADENCJA_START = date(2021, 10, 26)
KADENCJA_END = date(2026, 10, 25)
WP_NUMBER = 8

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Force unbuffered output. NAS subprocess captures stdout/stderr i czasami
# bufferuje mimo PYTHONUNBUFFERED=1 w Dockerfile (zależy od subprocess.run
# kwargs). Bez tego print po headerze nigdy nie dociera do logu pipeline'u
# póki proces nie skończy, więc user widzi półgodzinną ciszę.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


def log(msg: str) -> None:
    """Print z natychmiastowym flushem do stderr."""
    print(msg, file=sys.stderr, flush=True)


def setup_watchdog(timeout_seconds: int = 600) -> None:
    """SIGALRM jeśli scraper przekroczy timeout. Hard kill cały proces.

    Bez tego pdfplumber/requests potrafią wisieć w nieskończoność na
    pojedynczym PDF i NAS pipeline blokuje resztę miast/sejmików.
    """
    import signal

    def _handler(signum, frame):
        log(f"\n✗ WATCHDOG: scraper przekroczył {timeout_seconds}s. ABORT.")
        sys.exit(124)

    try:
        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(timeout_seconds)
    except (AttributeError, ValueError):
        # Brak SIGALRM (Windows) — bez watchdoga
        pass

# Niemieckie miesiące w slug formie używanej przez landtag-mv.de
MONTH_SLUGS = [
    "januar", "februar", "maerz", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "dezember",
]
MONTH_NUM_FROM_SLUG = {slug: i + 1 for i, slug in enumerate(MONTH_SLUGS)}

# Header w PDF Votebox: "Sitzung: 117 (am 10.10.2025 um 09:01 Uhr)"
HEADER_DATE_RE = re.compile(
    r"Sitzung:\s*(\d+).*?am\s+(\d{1,2}\.\d{1,2}\.\d{4})", re.DOTALL
)
HEADER_DRUCKSACHE_RE = re.compile(r"Drucksache[:\s]+([\w/\-\.]+)")
HEADER_TOP_RE = re.compile(r"TOP\s+(\d+)")
HEADER_RESULT_RE = re.compile(
    r"(Abgelehnt|Beschlossen|Mehrheitlich angenommen|Angenommen)"
)
HEADER_TITLE_RE = re.compile(
    r"Gegenstand der Abstimmung[:\s]*(.+?)(?=\n\n|\nJa\b|\nNein\b)", re.DOTALL
)

# Sekcje listy imiennej: "Ja", "Nein", "Enthaltung", "Nicht abgestimmt"
SECTION_NAMES = {
    "Ja": "za",
    "Nein": "przeciw",
    "Enthaltung": "wstrzymal_sie",
    "Nicht abgestimmt": "brak_glosu",
}

# Votebox ma DWA szablony PDF (potwierdzone na realnych plikach 8. WP):
#   1. "ABSTIMMUNGSPROTOKOLL" (np. 117. Sitzung) — czyste nagłówki sekcji
#      "Ja 28 Stimmen" w osobnych liniach + nazwiska rozdzielone średnikami
#      z przecinkiem: "(AfD) de Jesus-Fernandes, Thomas; (AfD) Federau, Petra".
#   2. "ABSTIMMUNGSERGEBNIS" (np. 98. Sitzung) — nagłówki tylko jako linia
#      podsumowania z procentem "Ja 43,94% 29 Stimmen", nazwiska BEZ przecinka
#      w osobnych liniach, układ dwukolumnowy. Z czystego tekstu pdfplumbera
#      kolejność czytania jest pomieszana i nie da się pewnie przypisać nazwisk
#      do sekcji — dlatego dla tego szablonu bierzemy tylko liczniki zbiorcze
#      (counts) z nagłówka, a listy imienne walidujemy i odrzucamy, jeśli nie
#      zgadzają się z licznikami (patrz parse_votebox_text + names_reliable).

# Wpis pojedynczego posła w sekcji. Body przetwarzamy osobno (przecinek
# opcjonalny), więc tu łapiemy tylko "(Fraktion) reszta".
ENTRY_RE = re.compile(r"\(([^)]+)\)\s*(.+)", re.DOTALL)

# Stary monolityczny NAME_RE (wymagał przecinka) — zostawiony dla zgodności
# z ewentualnymi importami, ale parser już go nie używa.
NAME_RE = re.compile(r"\(([^)]+)\)\s*([\wÄÖÜäöüß\-\.\s\(\)]+?,\s*[\wÄÖÜäöüß\-\.\s]+)")
# Tytuł akademicki w nawiasach po nazwisku (Prof. Dr.) lub (Dr.) - usuwamy
# żeby dwóch posłów o tym samym nazwisku nie pojawiało się jako różni.
TITLE_PAREN_RE = re.compile(r"\s*\([^)]+\)\s*")

# Liczniki zbiorcze z linii podsumowania. Tolerancyjne na: opcjonalny ":"/"-",
# opcjonalny procent ("43,94%" albo "43,08 %"), opcjonalne "Stimmen".
# Działa dla obu szablonów Votebox. Każda etykieta brana z PIERWSZEGO
# wystąpienia (to zawsze blok podsumowania na górze protokołu).
COUNT_LABELS = {
    "Ja": "za",
    "Nein": "przeciw",
    "Enthaltung": "wstrzymal_sie",
    "Nicht abgestimmt": "brak_glosu",
}


def _count_re(label: str) -> re.Pattern:
    return re.compile(
        r"(?<![A-Za-zÄÖÜäöüß])" + re.escape(label) +
        r"\s*[:\-]?\s*"
        r"(?:\d{1,3}(?:[.,]\d+)?\s*%\s*)?"   # opcjonalny procent
        r"(\d{1,3})(?![\d.,])"                # liczba głosów
    )


_COUNT_PATTERNS = {label: _count_re(label) for label in COUNT_LABELS}


def extract_section_counts(text: str) -> dict[str, int]:
    """Wyciągnij liczniki za/przeciw/wstrzymal_sie/brak_glosu z nagłówka.

    Pewne źródło wyniku niezależnie od tego, czy listy imienne dadzą się
    sparsować. "Nicht abgestimmt" sprawdzane PRZED "Ja"/"Nein", a literał
    zawiera "Nicht ", więc nie myli się z nagłówkowym "Abgestimmt:".
    """
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    for label, cat in COUNT_LABELS.items():
        m = _COUNT_PATTERNS[label].search(text)
        if m:
            counts[cat] = int(m.group(1))
    return counts


def fetch(url: str, timeout: int = 15) -> str:
    """GET HTML, throw on non-2xx. Timeout 15s żeby nie wisieć 30s × 56 mcy."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return r.content


def list_archive_months(start: date, end: date) -> list[str]:
    """Wygeneruj listę slugów miesiąca dla pełnego zakresu kadencji.

    Landtag MV używa formatu `{miesiac-slug}-{rok}` np. `oktober-2025`.
    """
    out = []
    cur = date(start.year, start.month, 1)
    today = date.today()
    last = min(end, today)
    while cur <= last:
        slug = f"{MONTH_SLUGS[cur.month - 1]}-{cur.year}"
        out.append(slug)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def fetch_month_pdfs(slug: str, cache_dir: Path | None = None) -> list[str]:
    """Pobierz stronę archiwum miesiąca, wyciągnij linki PDF namentliche.

    Strona ma sekcję `<h4>Namentliche Abstimmungen</h4>` z listą `<a>`
    do plików `/fileadmin/.../Namentliche_Abstimmungen/YYYY-MM-DD-NNN._
    Sitzung_Namentliche_Abstimmung_zu_TOP_NN.pdf`. Sekcja może być
    pusta, wtedy [].
    """
    url = ARCHIVE_BASE + slug
    cache_file = None
    if cache_dir is not None:
        cache_file = cache_dir / f"month_{slug}.html"
        if cache_file.exists() and cache_file.stat().st_size > 100:
            html = cache_file.read_text(encoding="utf-8")
        else:
            html = fetch(url)
            cache_file.write_text(html, encoding="utf-8")
    else:
        html = fetch(url)

    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "Namentliche_Abstimmungen" not in href:
            continue
        if not href.lower().endswith(".pdf"):
            continue
        full = href if href.startswith("http") else BASE + href
        if full not in out:
            out.append(full)
    return out


def parse_pdf_filename(url: str) -> dict | None:
    """Wyciąg date/session_number/top z URL PDF.

    Realne warianty w archiwum landtag-mv.de:
      * `2025-10-10-117._Sitzung_Namentliche_Abstimmung_zu_TOP_35.pdf`
        (dash + kropka + underscore przed Sitzung — pierwotne założenie)
      * `2025-06-26_109.Sitzung_Namentliche_Abstimmung_zu_TOP_22.pdf`
        (underscore + kropka bez underscore przed Sitzung)
      * `2025-04-11_104._Sitzung_Namentliche_Abstimmung_zu_Zusatz-TOP_1.pdf`
        (Zusatz-TOP zamiast TOP)
      * `2025-01-31_98._Sitzung_Namentliche_Abstimmung_zu_TOP_32.pdf`
        (krótszy numer sesji 98)

    Regex tolerujący wszystkie 3 warianty: separator data/numer może być
    `-` albo `_`, po numerze 1-2 znaków `.` / `._`, opcjonalny `Zusatz-`
    przed TOP, TOP może mieć sufiks literowy (TOP_22a).
    """
    fname = url.rsplit("/", 1)[-1]
    m = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[-_](\d+)[._]+Sitzung_Namentliche_Abstimmung_zu_(?:Zusatz[-_])?TOP_(\d+[a-z]*\d*)\.pdf",
        fname,
        re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "session_date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
        "session_number": m.group(4),
        "top": m.group(5),
        "filename": fname,
        "is_zusatz": "Zusatz" in fname,
    }


def _extract_pdf_text_safe(pdf_path: Path) -> str:
    """Wyciąg tekstu z PDF, najpierw pdfplumber, fallback pypdf.

    pdfplumber czasami wisi na konkretnych PDF (Votebox MV miał case'y
    gdzie pdfplumber.open() blokował proces). Fallback na pypdf (prostszy,
    szybszy, bardziej tolerancyjny) jeśli pdfplumber rzuci wyjątek.
    """
    try:
        text_lines = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                text_lines.append(t)
        return "\n".join(text_lines)
    except Exception as e:
        log(f"    pdfplumber padł ({e}), fallback do pypdf")
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_path))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e2:
            log(f"    pypdf też padł ({e2}), zwracam pusty tekst")
            return ""


# Nagłówek sekcji listy imiennej w szablonie PROTOKOLL: osobna linia
# "Ja 28 Stimmen" (BEZ procentu — linia podsumowania "Ja 43,08 % 28 Stimmen"
# jest świadomie pomijana, bo ma "%").
SECTION_HEADER_RE = re.compile(
    r"^(Ja|Nein|Enthaltung|Nicht abgestimmt)"
    r"\s*[:\-]?\s*"
    r"(\d+)"
    r"(?:\s+Stimmen)?"
    r"\s*$",
    re.MULTILINE,
)

# Temat: linia "Antrag/Gesetzentwurf/... ... Drucksache N/NNNN".
TOPIC_ALT_RE = re.compile(
    r"((?:Antrag|Gesetzentwurf|Beschlussempfehlung|Entschlie[ßs]ungsantrag|"
    r"Änderungsantrag|Unterrichtung)\b.+?)(?:[-–]\s*)?Drucksache",
    re.DOTALL,
)


def _normalize_name(fraktion: str, body: str) -> str:
    """'(Fraktion) Nachname, Vorname' / 'Nachname Vorname' -> 'Vorname Nachname'.

    Tytuły akademickie w nawiasach ((Dr.), (Prof. Dr.)) usuwamy, żeby ten sam
    poseł nie pojawiał się jako dwie osoby. Bez przecinka (szablon ERGEBNIS)
    zostawiamy surowo — i tak walidacja odrzuci te listy, bo z dwukolumnowego
    tekstu nie da się pewnie przypisać nazwisk do sekcji.
    """
    body = re.sub(r"\s+", " ", body).strip().rstrip(";").strip()
    if "," in body:
        nach, vor = [s.strip() for s in body.split(",", 1)]
        nach = TITLE_PAREN_RE.sub(" ", nach).strip()
        vor = TITLE_PAREN_RE.sub(" ", vor).strip()
        return f"{vor} {nach}".strip()
    return TITLE_PAREN_RE.sub(" ", body).strip()


def _strip_noise(text: str) -> str:
    """Usuń stopkę Votebox i nagłówki strony, które zaśmiecają listy nazwisk.

    Bez tego ostatni wpis sekcji wciągał stopkę ("... Martina\\nElektronische
    Abstimmung über Votebox ...") i tworzył nazwisko-śmiecia — dokładnie taki
    garbage trafił wcześniej do club_assignments.
    """
    text = re.sub(r"Elektronische Abstimmung über Votebox[^\n]*", "", text)
    text = re.sub(
        r"^\s*(?:LANDTAG MECKLENBURG-VORPOMMERN|"
        r"ABSTIMMUNGS(?:PROTOKOLL|ERGEBNIS))\s*$",
        "", text, flags=re.MULTILINE,
    )
    text = re.sub(r"^\s*\d{1,3}\.?\s*[Ss]itzung\b[^\n]*$", "", text, flags=re.MULTILINE)
    return text


def parse_votebox_text(text: str) -> dict:
    """Czysty parser tekstu protokołu Votebox (testowalny bez PDF/sieci).

    Obsługuje OBA szablony Votebox:
      * liczniki (counts) bierze z nagłówka podsumowania — pewne dla obu
        szablonów (extract_section_counts),
      * listy imienne parsuje z sekcji (szablon PROTOKOLL),
      * waliduje: jeśli liczba nazwisk w którejś sekcji != licznik z nagłówka,
        ustawia names_reliable=False i ZWRACA PUSTE listy imienne — lepiej
        pokazać sam wynik zbiorczy niż błędne / zerowe nazwiska (regresja,
        która psuła 4 z 5 głosowań szablonu ERGEBNIS).

    Zwraca dict: topic, drucksache, result, counts, named_votes,
    councilor_clubs, names_reliable.
    """
    text = _strip_noise(text)

    drucksache_m = HEADER_DRUCKSACHE_RE.search(text)
    drucksache = drucksache_m.group(1).strip() if drucksache_m else None
    result_m = HEADER_RESULT_RE.search(text)
    result = result_m.group(1).strip() if result_m else None

    topic = ""
    title_m = HEADER_TITLE_RE.search(text)
    if title_m:
        topic = title_m.group(1)
    else:
        alt_m = TOPIC_ALT_RE.search(text)
        if alt_m:
            topic = alt_m.group(1)
    topic = re.sub(r"\s+", " ", topic).strip()[:300]

    # 1) Liczniki zbiorcze — pewne źródło wyniku.
    counts = extract_section_counts(text)

    # 2) Listy imienne (szablon PROTOKOLL: nagłówki sekcji + body ze średnikami).
    named_votes: dict[str, list[str]] = {
        "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []
    }
    councilor_clubs: dict[str, str] = {}

    matches = list(SECTION_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        target_cat = SECTION_NAMES.get(m.group(1))
        if not target_cat:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end]
        # Wpisy rozdzielone średnikami. Newline NIE jest separatorem — bywa
        # zawinięciem w środku wpisu ("(AfD) Reuken, \nStephan J.;").
        for piece in chunk.split(";"):
            em = ENTRY_RE.match(piece.strip())
            if not em:
                continue
            fraktion = em.group(1).strip()
            name = _normalize_name(fraktion, em.group(2))
            if not name:
                continue
            named_votes[target_cat].append(name)
            councilor_clubs[name] = fraktion

    # 3) Walidacja: liczba nazwisk per sekcja musi zgadzać się z licznikiem.
    names_reliable = all(
        len(named_votes[cat]) == counts[cat]
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu")
    )
    # Dodatkowy bezpiecznik: nie ufaj pojedynczym strzępkom (np. 1 fantomowy
    # głos przy zerowych licznikach), które dawały efekt "counts = same zera".
    if not names_reliable:
        named_votes = {k: [] for k in named_votes}
        councilor_clubs = {}

    return {
        "topic": topic or (f"Abstimmung Drs. {drucksache}" if drucksache else "Namentliche Abstimmung"),
        "drucksache": drucksache,
        "result": result,
        "counts": counts,
        "named_votes": named_votes,
        "councilor_clubs": councilor_clubs,
        "names_reliable": names_reliable,
    }


def parse_pdf_text(pdf_path: Path) -> dict:
    """Wrapper: wyciąga tekst z PDF i deleguje do parse_votebox_text."""
    text = _extract_pdf_text_safe(pdf_path)
    return parse_votebox_text(text)


def passed_from_counts(counts: dict, result_str: str | None) -> bool | None:
    """Determine pass/fail from counts and PDF header text."""
    if result_str:
        if result_str.startswith("Abgelehnt"):
            return False
        if result_str.startswith("Beschloss") or "angenommen" in result_str.lower():
            return True
    za = counts.get("za", 0)
    przeciw = counts.get("przeciw", 0)
    if za + przeciw == 0:
        return None
    return za > przeciw


def build_kadencja(
    cache_dir: Path | None,
    limit_sessions: int | None = None,
) -> dict:
    """Top-level scrape: enumerate months, fetch each PDF, build kadencja struct."""
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: sprawdź konektywność do landtag-mv.de zanim wszystkie 56
    # miesięcy będą timeout'ować po 15s każdy.
    log("Preflight: sprawdzam landtag-mv.de...")
    try:
        r = requests.get(BASE + "/", headers={"User-Agent": USER_AGENT}, timeout=10)
        log(f"  landtag-mv.de odpowiada HTTP {r.status_code}")
    except requests.Timeout:
        log("  ✗ landtag-mv.de timeout 10s. NAS prawdopodobnie nie ma dostępu.")
        return {
            "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
            "sessions": [], "total_sessions": 0, "total_votes": 0,
            "total_councilors": 0, "councilors": [], "votes": [],
            "similarity_top": [], "similarity_bottom": [],
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": BASE,
        }
    except Exception as e:
        log(f"  ✗ landtag-mv.de error: {type(e).__name__}: {e}")
        return {
            "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
            "sessions": [], "total_sessions": 0, "total_votes": 0,
            "total_councilors": 0, "councilors": [], "votes": [],
            "similarity_top": [], "similarity_bottom": [],
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": BASE,
        }

    months = list_archive_months(KADENCJA_START, KADENCJA_END)
    log(f"Skanuję {len(months)} miesięcy archiwum landtag-mv.de...")

    all_pdf_urls: list[str] = []
    consecutive_failures = 0
    for idx, slug in enumerate(months, start=1):
        prefix = f"[{idx}/{len(months)}] {slug}"
        try:
            urls = fetch_month_pdfs(slug, cache_dir)
            consecutive_failures = 0
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                log(f"  {prefix}: 404 (brak strony)")
                continue
            log(f"  {prefix}: HTTP {e}")
            consecutive_failures += 1
        except requests.Timeout:
            log(f"  {prefix}: timeout 15s")
            consecutive_failures += 1
            if consecutive_failures >= 3:
                log(
                    "  ABORT: 3 kolejne timeouty na landtag-mv.de. "
                    "Sprawdź konektywność z NAS-a. Zostaw cache na dysku, "
                    "kolejny run podejmie tam gdzie się skończył."
                )
                break
            continue
        except Exception as e:
            log(f"  {prefix}: {type(e).__name__}: {e}")
            consecutive_failures += 1
            continue
        if urls:
            log(f"  {prefix}: {len(urls)} PDFów namentliche")
            all_pdf_urls.extend(urls)
        else:
            log(f"  {prefix}: brak namentliche")
        time.sleep(0.15)

    # Dedup
    seen = set()
    pdf_urls_unique = []
    for u in all_pdf_urls:
        if u not in seen:
            seen.add(u)
            pdf_urls_unique.append(u)

    log(f"Razem {len(pdf_urls_unique)} unikalnych PDFów namentliche")

    if limit_sessions:
        pdf_urls_unique = pdf_urls_unique[:limit_sessions]
        log(f"  ograniczono do {len(pdf_urls_unique)} (--limit)")

    votes: list[dict] = []
    sessions: dict[tuple[str, str], dict] = {}
    all_councilors: dict[str, str] = {}  # name -> fraktion (last seen wins)

    for pdf_idx, pdf_url in enumerate(pdf_urls_unique, start=1):
        meta = parse_pdf_filename(pdf_url)
        if not meta:
            log(f"  [{pdf_idx}/{len(pdf_urls_unique)}] WARN: nie parsuję nazwy {pdf_url}")
            continue
        log(f"  [{pdf_idx}/{len(pdf_urls_unique)}] {meta['filename']}")

        sess_key = (meta["session_date"], meta["session_number"])
        if sess_key not in sessions:
            sessions[sess_key] = {
                "date": meta["session_date"],
                "number": meta["session_number"],
                "vote_count": 0,
                "attendees": set(),
            }

        # Pobierz PDF i cache na dysku
        pdf_local = None
        if cache_dir:
            url_hash = md5(pdf_url.encode("utf-8")).hexdigest()[:12]
            pdf_local = cache_dir / f"pdf_{url_hash}_{meta['filename']}"
            if pdf_local.exists() and pdf_local.stat().st_size >= 1000:
                log(f"    cache hit ({pdf_local.stat().st_size} B)")
            else:
                log(f"    fetch...")
                t0 = time.time()
                try:
                    pdf_local.write_bytes(fetch_bytes(pdf_url))
                except Exception as e:
                    log(f"  ERR pobierania {meta['filename']}: {e}")
                    continue
                log(f"    fetched {pdf_local.stat().st_size} B in {time.time()-t0:.1f}s")
                time.sleep(0.2)
        else:
            log(f"    fetch (no cache)...")
            t0 = time.time()
            try:
                content = fetch_bytes(pdf_url)
            except Exception as e:
                log(f"  ERR pobierania {meta['filename']}: {e}")
                continue
            log(f"    fetched {len(content)} B in {time.time()-t0:.1f}s")
            import tempfile
            pdf_local = Path(tempfile.mktemp(suffix=".pdf"))
            pdf_local.write_bytes(content)

        log(f"    parse...")
        t0 = time.time()
        try:
            parsed = parse_pdf_text(pdf_local)
        except Exception as e:
            log(f"  ERR parsowania {meta['filename']}: {e}")
            continue
        total_named = sum(len(v) for v in parsed['named_votes'].values())
        log(f"    parsed in {time.time()-t0:.1f}s, {total_named} głosów imiennych")

        # Debug dump: jeśli listy imienne nie przeszły walidacji względem
        # liczników (nieobsługiwany / dwukolumnowy szablon), zapisz PDF text
        # żeby zdiagnozować format. Limit 5 sampleów per run.
        if not parsed.get("names_reliable", True) and cache_dir:
            debug_dir = cache_dir / "parse_debug_unparseable"
            debug_dir.mkdir(exist_ok=True)
            existing = list(debug_dir.glob("*.txt"))
            if len(existing) < 5:
                try:
                    sample_text = _extract_pdf_text_safe(pdf_local)
                    debug_path = debug_dir / f"{meta['filename']}.txt"
                    debug_path.write_text(sample_text[:3000], encoding="utf-8")
                    log(f"    [debug] zapisano sample tekst do {debug_path}")
                except Exception:
                    pass

        # Liczniki bierzemy z nagłówka (pewne dla obu szablonów Votebox), NIE
        # z długości list imiennych — inaczej szablon ERGEBNIS dawał same zera.
        counts = parsed["counts"]
        names_reliable = parsed.get("names_reliable", True)
        if not names_reliable:
            log(
                f"    UWAGA: listy imienne niespójne z licznikami "
                f"(counts={counts}) — zapisuję sam wynik zbiorczy bez nazwisk "
                f"[{meta['filename']}]"
            )
        # Vote ID: data_TOP-numer (np. 2025-10-10_TOP35)
        vote_id = f"{meta['session_date']}_TOP{meta['top']}"

        # Topic: jeśli PDF parser nie wyłapał, użyj drucksache lub fallback
        topic = parsed["topic"]
        if not topic or topic == "Namentliche Abstimmung":
            topic = f"TOP {meta['top']}"
            if parsed["drucksache"]:
                topic += f" Drs. {parsed['drucksache']}"

        votes.append({
            "id": vote_id,
            "source_url": pdf_url,
            "session_date": meta["session_date"],
            "session_number": meta["session_number"],
            "topic": topic,
            "druk": parsed.get("drucksache"),
            "resolution": parsed.get("result"),
            "counts": counts,
            "named_votes": parsed["named_votes"],
            "named_votes_available": names_reliable,
            "passed": passed_from_counts(counts, parsed.get("result")),
        })

        # Track frekwencja per session
        for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
            sessions[sess_key]["attendees"].update(parsed["named_votes"][cat])
        sessions[sess_key]["vote_count"] += 1

        # Update club lookup
        for name, fraktion in parsed["councilor_clubs"].items():
            all_councilors[name] = fraktion

    log(f"Sparsowano {len(votes)} głosowań z {len(sessions)} sesji, {len(all_councilors)} posłów")

    # councilor_index: posortowana lista unikalnych nazwisk z poprawnie
    # sparsowanych głosowań (tylko te które mają przypisaną Fraktion).
    # build_assembly_metrics.py oczekuje tej listy i indeksów całkowitoliczbowych
    # w named_votes — analogicznie do sejmików województw.
    councilor_index: list[str] = sorted(all_councilors.keys())
    name_to_idx: dict[str, int] = {n: i for i, n in enumerate(councilor_index)}

    # Konwertuj named_votes z list stringów na listy indeksów.
    # Nazwy nieznane (np. niepoprawnie sparsowane garbage strings z format PDF
    # bez (Fraktion) prefix) są pomijane — nie trafiają do indeksu.
    for v in votes:
        nv_indexed: dict[str, list[int]] = {}
        for cat, names in v["named_votes"].items():
            indices = []
            for n in names:
                idx = name_to_idx.get(n)
                if idx is not None:
                    indices.append(idx)
            nv_indexed[cat] = indices
        v["named_votes"] = nv_indexed

    # Build sessions list (sorted)
    sessions_list = []
    for (sdate, snum), s in sorted(sessions.items(), key=lambda x: x[0]):
        sessions_list.append({
            "date": sdate,
            "number": snum,
            "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]),
            "attendees": sorted(s["attendees"]),
        })

    # Club counts (z councilor_index + all_councilors)
    club_counts: dict[str, int] = defaultdict(int)
    for name in councilor_index:
        club_counts[all_councilors.get(name, "?")] += 1

    return {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "clubs": {club: cnt for club, cnt in sorted(club_counts.items())},
        "sessions": sessions_list,
        "total_sessions": len(sessions_list),
        "total_votes": len(votes),
        "total_councilors": len(councilor_index),
        "councilors": [],
        "councilor_index": councilor_index,
        "votes": votes,
        "similarity_top": [],
        "similarity_bottom": [],
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": BASE,
        # Klucz prywatny (prefiks _) — nie trafia do kadencja JSON.
        # Używany przez main() do aktualizacji config.json["club_assignments"].
        "_club_assignments": dict(all_councilors),
    }


def _build_councilors(votes: list[dict], club_map: dict[str, str]) -> list[dict]:
    """Zbuduj listę posłów z statystykami frekwencji/aktywności."""
    all_names = set()
    for v in votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    sessions_with_votes = set(v["session_date"] for v in votes)
    total_sessions = len(sessions_with_votes)
    total_votes = len(votes)

    councilor_data: dict[str, dict] = {}
    for name in all_names:
        councilor_data[name] = {
            "name": name,
            "club": club_map.get(name, "?"),
            "votes_za": 0,
            "votes_przeciw": 0,
            "votes_wstrzymal": 0,
            "votes_brak": 0,
            "votes_nieobecny": 0,
            "sessions_present": set(),
        }

    for v in votes:
        for name in v["named_votes"].get("za", []):
            if name in councilor_data:
                councilor_data[name]["votes_za"] += 1
                councilor_data[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("przeciw", []):
            if name in councilor_data:
                councilor_data[name]["votes_przeciw"] += 1
                councilor_data[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("wstrzymal_sie", []):
            if name in councilor_data:
                councilor_data[name]["votes_wstrzymal"] += 1
                councilor_data[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("brak_glosu", []):
            if name in councilor_data:
                councilor_data[name]["votes_brak"] += 1
                councilor_data[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("nieobecni", []):
            if name in councilor_data:
                councilor_data[name]["votes_nieobecny"] += 1

    result = []
    for c in councilor_data.values():
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        frekwencja = (len(c["sessions_present"]) / total_sessions * 100) if total_sessions else 0
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        result.append({
            "name": c["name"],
            "club": c["club"],
            "frekwencja": round(frekwencja, 1),
            "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0,
            "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": 0,
            "rebellions": [],
            "has_activity_data": False,
            "activity": None,
        })

    return sorted(result, key=lambda c: c["name"])


def save_split_output(kadencja: dict, out_path: Path) -> None:
    """Zapisz data.json (index) + kadencja-{id}.json zgodnie ze schematem Radoskop."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kid = kadencja["id"]
    kad_path = out_path.parent / f"kadencja-{kid}.json"
    # Usuń klucze prywatne (prefiks _) przed zapisem do JSON.
    kad_to_save = {k: v for k, v in kadencja.items() if not k.startswith("_")}
    with kad_path.open("w", encoding="utf-8") as f:
        json.dump(kad_to_save, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [{"id": kid, "label": kadencja["label"]}],
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Landtag Mecklenburg-Vorpommern")
    parser.add_argument("--cache", type=Path, default=Path(".cache/landtag-mv"))
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("docs/data.json"))
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit PDFów (debug)")
    args = parser.parse_args()

    log("=== Radoskop scraper: Landtag Mecklenburg-Vorpommern ===")
    log(f"  cache: {args.cache}")
    log(f"  output: {args.output}")

    # 5-minutowy watchdog. Bez tego pdfplumber albo requests potrafią
    # zawiesić proces w nieskończoność na pojedynczym PDF.
    setup_watchdog(timeout_seconds=300)

    kadencja = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit)

    # Guard: jeśli żadnych głosowań, nie nadpisuj istniejących
    if kadencja["total_votes"] == 0 and (args.output.parent / f"kadencja-{KADENCJA_ID}.json").exists():
        log(f"\n✗ Zero głosowań, nie nadpisuję {args.output}")
        return 1

    # Zaktualizuj club_assignments w config.json na podstawie zebranych frakcji.
    # build_assembly_metrics.py czyta frakcje właśnie z config["club_assignments"],
    # więc bez tego wszyscy posłowie mają klub "?".
    config_path = args.output.parent.parent / "config.json"
    if config_path.is_file() and kadencja.get("councilor_index"):
        try:
            with config_path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            # Poszerzaj istniejące przypisania (nie kasuj ręcznych poprawek).
            existing = cfg.get("club_assignments", {})
            new_assignments = kadencja.get("_club_assignments", {})
            if new_assignments:
                merged = {**new_assignments, **existing}  # existing wygrywa
                cfg["club_assignments"] = merged
                with config_path.open("w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                log(f"  Zaktualizowano club_assignments ({len(merged)} posłów) w config.json")
        except Exception as e:
            log(f"  WARN: nie można zaktualizować config.json: {e}")

    save_split_output(kadencja, args.output)

    log(f"\n✓ Zapisano {args.output}")
    log(f"  Sesji: {kadencja['total_sessions']}")
    log(f"  Głosowań: {kadencja['total_votes']}")
    log(f"  Posłów: {kadencja['total_councilors']}")
    log(f"  Frakcje: {dict(kadencja['clubs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
