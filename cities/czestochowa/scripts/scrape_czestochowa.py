#!/usr/bin/env python3
"""
Radoskop Częstochowa — BIP scraper.

eSesja Częstochowy NIE udostępnia głosowań (sprawdzone 2026-05-13:
https://czestochowa.esesja.pl/glosowania → "Archiwum przeprowadzonych głosowań" puste).
BIP Częstochowy publikuje "Wykaz głosowań" jako PDF per sesja na stronie
detalu sesji. Ten scraper:

  1. Pobiera listę sesji z https://bip.czestochowa.pl/artykuly/71761/sesje-rady-miasta-ix-kadencji
     (paginated, ~10 per page).
  2. Dla każdej sesji fetchuje stronę detalu, znajduje załącznik "Wykaz głosowań" (.pdf).
  3. Pobiera PDF do cache (sha256 invalidation).
  4. Parsuje PDF pdfplumber'em, wyciąga: numer głosowania, temat, wyniki imienne
     (radny → ZA/PRZECIW/WSTRZYMAŁ/NIEOBECNY).
  5. Składa data.json + profiles.json + kadencja-{id}.json w standardowym formacie Radoskop.

Cache na trzech poziomach:
  - HTML pages (--cache-dir, opcjonalny)
  - PDF files ({pdf_dir}/{sha256-of-url}.pdf, persystentne)
  - Parsed votes ({pdf_dir}/../parsed_votes/{sha256-of-pdf}.json)

PDF parser ma fallback: gdy heurystyki nie wykryją tabeli głosów, log warning
i zapisuje [] dla danej sesji. Po pierwszym runie warto sprawdzić log
i adjustować parser pod faktyczny format PDF Częstochowy.

Użycie:
  python3 scrape_czestochowa.py --output docs/data.json --profiles docs/profiles.json
                                [--pdf-dir <scratch>/pdfs] [--cache-dir <scratch>/.cache/html]
                                [--max-sessions N] [--debug]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)
try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4")
    sys.exit(1)
try:
    import pdfplumber
except ImportError:
    print("Zainstaluj: pip install pdfplumber")
    sys.exit(1)


BIP_BASE = "https://bip.czestochowa.pl"
SESSIONS_LIST_URL = f"{BIP_BASE}/artykuly/71761/sesje-rady-miasta-ix-kadencji"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}
DEFAULT_KADENCJA = "2024-2029"

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://czestochowa.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,*/*",
}
DELAY = 0.3
TIMEOUT = 30

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}

# Skład Rady Miasta Częstochowy IX kadencji (25 radnych) wg BIP
# https://bip.czestochowa.pl/artykuly/71763/kluby-radnych (stan 2026-05-13).
# Format: "Imię Nazwisko" → kod klubu. PDF z BIP może podawać też w formie
# "NAZWISKO Imię" (caps), normalizujemy w build_name_lookup().
COUNCILORS: dict[str, str] = {
    # KO — Koalicja Obywatelska (8)
    "Joanna Rekwirewicz": "KO",
    "Łukasz Banaś": "KO",
    "Marcin Biernat": "KO",
    "Barbara Gieroń": "KO",
    "Marcin Korzeniec": "KO",
    "Marcin Maranda": "KO",
    "Marta Salwierak": "KO",
    "Zofia Wojtysiak-Kowalik": "KO",
    # Lewica (6)
    "Dariusz Kapinos": "Lewica",
    "Zbigniew Niesmaczny": "Lewica",
    "Tomasz Blukacz": "Lewica",
    "Małgorzata Iżyńska": "Lewica",
    "Ewa Lewandowska": "Lewica",
    "Michał Lewandowski": "Lewica",
    # PiS (9)
    "Paweł Ruksza": "PiS",
    "Monika Pohorecka-Całko": "PiS",
    "Katarzyna Jastrzębska": "PiS",
    "Robert Leciński": "PiS",
    "Alan Piotrowski": "PiS",
    "Karolina Stępień": "PiS",
    "Beata Struzik": "PiS",
    "Artur Warzocha": "PiS",
    "Piotr Wrona": "PiS",
    # NZ (2)
    "Krystyna Stefańska": "NZ",
    "Krzysztof Świerczyński": "NZ",
}


# ---------------------------------------------------------------------------
# Helpers: HTTP + cache
# ---------------------------------------------------------------------------

_CACHE_DIR: Path | None = None


def init_cache(path: str | None) -> None:
    global _CACHE_DIR
    _CACHE_DIR = Path(path) if path else None
    if _CACHE_DIR:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(url: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.html"


def fetch_html(http_session: requests.Session, url: str, use_cache: bool = True, debug: bool = False) -> str:
    cache_p = _cache_path(url) if use_cache else None
    if cache_p and cache_p.exists():
        if debug:
            print(f"  [cache] {url}")
        return cache_p.read_text(encoding="utf-8")
    if debug:
        print(f"  GET {url}")
    resp = http_session.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    text = resp.text
    if cache_p:
        cache_p.write_text(text, encoding="utf-8")
    time.sleep(DELAY)
    return text


def fetch_pdf(http_session: requests.Session, url: str, pdf_dir: Path, debug: bool = False) -> Path | None:
    """Cache PDF po sha256(url) (URL stabilny dla danej wersji pliku w BIP)."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    out = pdf_dir / f"{key}.pdf"
    if out.exists() and out.stat().st_size > 0:
        if debug:
            print(f"      [cache] PDF {out.name}")
        return out
    if debug:
        print(f"      GET PDF {url}")
    try:
        resp = http_session.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        with out.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        time.sleep(DELAY)
        return out
    except Exception as exc:
        print(f"      BŁĄD pobierania {url}: {exc}")
        if out.exists():
            out.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# Name normalization (PDF może mieć "NAZWISKO Imię" caps)
# ---------------------------------------------------------------------------

def _normalize_name_for_match(name: str) -> str:
    parts = re.split(r"\s+", (name or "").strip().lower())
    parts = [p for p in parts if p]
    return " ".join(sorted(parts))


def build_name_lookup() -> dict[str, str]:
    out = {}
    for canonical_name in COUNCILORS:
        out[_normalize_name_for_match(canonical_name)] = canonical_name
    return out


_NAME_LOOKUP: dict[str, str] = {}


def resolve_canonical_name(name: str) -> str | None:
    return _NAME_LOOKUP.get(_normalize_name_for_match(name))


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    number: str
    date: str
    title: str
    detail_url: str
    nadzwyczajna: bool


SESSION_NUMBER_RE = re.compile(r"^([IVXLCDM]+)\s+(Zwyczajna|Nadzwyczajna)\s+Sesja", re.IGNORECASE)
DATE_RE = re.compile(
    r"(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)"
    r"\s+(?:\([a-zż]+\)\s+)?(\d{4})", re.IGNORECASE,
)


def fetch_session_list(http_session: requests.Session, debug: bool = False, max_sessions: int = 0) -> list[SessionMeta]:
    sessions: list[SessionMeta] = []
    page = 1
    seen_urls: set[str] = set()
    base_no_slug = SESSIONS_LIST_URL.replace("/sesje-rady-miasta-ix-kadencji", "")
    while True:
        url = f"{base_no_slug}/{page}/10/sesje-rady-miasta-ix-kadencji" if page > 1 else SESSIONS_LIST_URL
        try:
            html = fetch_html(http_session, url, use_cache=False, debug=debug)
        except Exception as exc:
            print(f"  BŁĄD strony {page}: {exc}")
            break
        soup = BeautifulSoup(html, "html.parser")

        before_count = len(sessions)
        for h2 in soup.find_all(["h2", "h3"]):
            a = h2.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if "/artykul/71761/" not in href:
                continue
            detail_url = href if href.startswith("http") else BIP_BASE + href
            if detail_url in seen_urls:
                continue
            title = a.get_text(strip=True)
            m = SESSION_NUMBER_RE.match(title)
            if not m:
                continue
            number = m.group(1).upper()
            nadzwyczajna = m.group(2).lower().startswith("nadzw")

            # Data sesji jest w <article><div><p>...</p></div></article>, gdzie
            # h2 leży w <header>. Sibling search z poziomu h2 nie zadziała bo
            # header nie ma rodzeństwa - trzeba przeszukać cały article.
            # Format strony BIP zmienił się ok. 2026-04 (przed: data w sibling).
            date_str = ""
            article = h2.find_parent("article")
            if article is not None:
                article_text = article.get_text(" ", strip=True)
                dm = DATE_RE.search(article_text)
                if dm:
                    day = int(dm.group(1))
                    month = MONTHS_PL[dm.group(2).lower()]
                    year = int(dm.group(3))
                    date_str = f"{year:04d}-{month:02d}-{day:02d}"
            # Fallback: szukamy daty w tytule (sesje nadzwyczajne czasem mają ją tam).
            if not date_str:
                dm = DATE_RE.search(title)
                if dm:
                    day = int(dm.group(1))
                    month = MONTHS_PL[dm.group(2).lower()]
                    year = int(dm.group(3))
                    date_str = f"{year:04d}-{month:02d}-{day:02d}"

            seen_urls.add(detail_url)
            sessions.append(SessionMeta(
                number=number, date=date_str, title=title,
                detail_url=detail_url, nadzwyczajna=nadzwyczajna,
            ))

        added = len(sessions) - before_count
        if debug:
            print(f"  Strona {page}: +{added} sesji (razem: {len(sessions)})")
        if added == 0:
            break
        if max_sessions and len(sessions) >= max_sessions:
            break

        next_link = soup.find("a", href=re.compile(rf"/artykuly/71761/{page + 1}/"))
        if not next_link:
            break
        page += 1

    return sessions[:max_sessions] if max_sessions else sessions


# ---------------------------------------------------------------------------
# Session detail: znajdź "Wykaz głosowań" PDF
# ---------------------------------------------------------------------------

def find_voting_pdf_url(http_session: requests.Session, detail_url: str, debug: bool = False) -> str | None:
    html = fetch_html(http_session, detail_url, use_cache=True, debug=debug)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=re.compile(r"/attachments/download/\d+")):
        text = a.get_text(" ", strip=True).lower()
        if ("wykaz" in text or "wyniki" in text) and "głos" in text:
            href = a["href"]
            return href if href.startswith("http") else BIP_BASE + href
    if debug:
        print(f"      brak 'Wykaz głosowań' w {detail_url}")
    return None


# ---------------------------------------------------------------------------
# PDF parser
# ---------------------------------------------------------------------------

VOTE_RESULT_TOKENS = {
    "za": "za",
    "przeciw": "przeciw",
    "wstrzymał": "wstrzymal_sie",
    "wstrzymał się": "wstrzymal_sie",
    "wstrzymała": "wstrzymal_sie",
    "wstrz.": "wstrzymal_sie",
    "nieobecny": "nieobecni",
    "nieobecna": "nieobecni",
    "nieobecność": "nieobecni",
    "brak": "brak_glosu",
    "brak głosu": "brak_glosu",
}


def _classify_vote_token(token: str) -> str | None:
    t = (token or "").strip().lower().rstrip(".,;:")
    return VOTE_RESULT_TOKENS.get(t)


# Format BIP Częstochowy (zweryfikowany 2026-05-30 na XXXII sesji): eSesja
# "Raport z głosowań". Każde głosowanie to jedna linia nagłówka:
#   "N. Głosowanie w sprawie <temat> - czas głosowania: <data>, godz. HH:MM,
#    wyniki: ZA: a, PRZECIW: b, WSTRZYMUJĘ SIĘ: c, BRAK GŁOSU: d, NIEOBECNI: e"
# po czym "Wyniki imienne: IMIĘ NAZWISKO (TOKEN), ...". Nazwiska WIELKIMI
# literami w kolejności "Imię Nazwisko", token w nawiasie.
_RAPORT_HEAD_RE = re.compile(
    r"(\d+)\.\s*Głosowanie\s+(?:w sprawie\s+)?(.*?)\s*-\s*czas głosowania:\s*(.*?),\s*"
    r"godz\.\s*[\d:]+\s*,\s*wyniki:\s*ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*"
    r"WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),\s*NIEOBECN[YI]:\s*(\d+)"
)
_RAPORT_PAIR_RE = re.compile(r"([^,()]+?)\s*\(([^)]+)\)")
_RAPORT_TOKEN = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIE": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu",
    "BRAK GLOSU": "brak_glosu",
    "NIEOBECNI": "nieobecni",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
}


def _parse_raport_pdf(full_text: str, session: SessionMeta, debug: bool = False) -> list[dict]:
    # Złącz dywizy rozbite końcem wiersza (np. "WOJTYSIAK-\nKOWALIK") i spłaszcz
    # białe znaki — nazwiska i tokeny zawijają się między wierszami/stronami.
    joined = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "-", full_text)
    flat = re.sub(r"\s+", " ", joined)
    heads = list(_RAPORT_HEAD_RE.finditer(flat))
    if not heads:
        return []
    starts = [m.start() for m in heads] + [len(flat)]
    votes: list[dict] = []
    for i, m in enumerate(heads):
        topic = m.group(2).strip(" .:-")
        cz, cp, cw, cb, cn = (int(x) for x in m.groups()[3:8])
        counts = {"za": cz, "przeciw": cp, "wstrzymal_sie": cw, "brak_glosu": cb, "nieobecni": cn}
        seg = flat[m.end():starts[i + 1]]
        seg = seg.split("Wyniki imienne:", 1)[1] if "Wyniki imienne:" in seg else ""
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
        for raw_name, token in _RAPORT_PAIR_RE.findall(seg):
            key = _RAPORT_TOKEN.get(token.strip().upper())
            if not key:
                continue
            canonical = resolve_canonical_name(re.sub(r"^[,\s]+", "", raw_name).strip())
            if canonical:
                named[key].append(canonical)
        vote_num = i + 1
        num_part = f"_{session.number}" if getattr(session, "number", "") else ""
        votes.append({
            "id": f"{session.date}{num_part}_{vote_num:03d}",
            "session_number": session.number,
            "session_date": session.date,
            "topic": topic[:300] if topic else f"Głosowanie nr {vote_num}",
            "counts": counts,
            "named_votes": named,
        })
    return votes


def parse_voting_pdf(pdf_path: Path, session: SessionMeta, debug: bool = False) -> list[dict]:
    """Parsuje 'Wykaz głosowań' PDF do listy głosowań w formacie Radoskop.

    Główna ścieżka: format eSesja "Raport z głosowań" (`_parse_raport_pdf`,
    zweryfikowany na XXXII sesji 2026). Starszy parser blokowy + tabelowy
    zostają jako fallback na wypadek zmiany formatu przez BIP.
    """
    votes: list[dict] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                t = page.extract_text() or ""
                full_text += t + "\n"
            if debug:
                print(f"      PDF text: {len(full_text)} znaków, {len(pdf.pages)} stron")
                preview = full_text[:600].replace("\n", " | ")
                print(f"      preview: {preview}")

            votes = _parse_raport_pdf(full_text, session, debug=debug)

            if not votes:
                vote_idx = 0
                for block in _split_into_vote_blocks(full_text):
                    vote_idx += 1
                    parsed = _parse_vote_block(block, session, vote_idx)
                    if parsed:
                        votes.append(parsed)

            if not votes:
                vote_idx = 0
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        parsed_table = _parse_vote_table(table, session, vote_idx + 1)
                        if parsed_table:
                            vote_idx += 1
                            votes.append(parsed_table)

    except Exception as exc:
        print(f"      BŁĄD parsowania PDF {pdf_path.name}: {exc}")
        return []

    if not votes and debug:
        print(f"      WARN: parser nie wykrył głosowań w {pdf_path.name}, sprawdź format")

    return votes


def _split_into_vote_blocks(text: str) -> list[str]:
    pattern = re.compile(
        r"^\s*(?:głosowanie|GŁOSOWANIE)\s*(?:nr\.?|Nr\.?)?\s*(\d+)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return []
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(text[start:end])
    return blocks


def _parse_vote_block(block: str, session: SessionMeta, vote_idx: int) -> dict | None:
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    if not lines:
        return None
    header = lines[0]
    m = re.match(
        r"^(?:głosowanie|GŁOSOWANIE)\s*(?:nr\.?|Nr\.?)?\s*(\d+)\s*[\.:\-]?\s*(.*)",
        header, re.IGNORECASE,
    )
    vote_num = vote_idx
    topic = ""
    if m:
        vote_num = int(m.group(1))
        topic = m.group(2).strip(" .:-")

    if not topic:
        topic_parts = []
        for line in lines[1:]:
            if re.search(r"\b(ZA|PRZECIW|WSTRZYM|NIEOBEC)\b", line, re.IGNORECASE):
                break
            topic_parts.append(line)
        topic = " ".join(topic_parts).strip()

    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    for label, key in [("za", "za"), ("przeciw", "przeciw"),
                       ("wstrzymał", "wstrzymal_sie"), ("wstrzym", "wstrzymal_sie"),
                       ("brak", "brak_glosu"), ("nieobecn", "nieobecni")]:
        m_count = re.search(rf"\b{label}\b\s*[:\-]?\s*(\d+)", block, re.IGNORECASE)
        if m_count:
            counts[key] = max(counts[key], int(m_count.group(1)))

    named_votes = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
    for line in lines:
        vm = re.search(
            r"\b(ZA|PRZECIW|WSTRZYMAŁ(?:\s+SIĘ)?|WSTRZYMAŁA(?:\s+SIĘ)?|NIEOBECNY|NIEOBECNA|BRAK)\b",
            line, re.IGNORECASE,
        )
        if not vm:
            continue
        vote_word = vm.group(1).upper()
        name_part = (line[:vm.start()] + " " + line[vm.end():]).strip()
        name_part = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", name_part).strip()
        if not name_part or len(name_part) > 80:
            continue
        canonical = resolve_canonical_name(name_part)
        if not canonical:
            continue
        if vote_word == "ZA":
            key = "za"
        elif vote_word == "PRZECIW":
            key = "przeciw"
        elif vote_word.startswith("WSTRZ"):
            key = "wstrzymal_sie"
        elif vote_word.startswith("NIEOBEC"):
            key = "nieobecni"
        elif vote_word == "BRAK":
            key = "brak_glosu"
        else:
            continue
        named_votes[key].append(canonical)

    total_named = sum(len(v) for v in named_votes.values())
    if total_named and not any(counts.values()):
        for k in counts:
            counts[k] = len(named_votes[k])

    vote_id = f"{session.date}_{vote_num:03d}"
    return {
        "id": vote_id,
        "session_number": session.number,
        "session_date": session.date,
        "topic": topic[:300] if topic else f"Głosowanie nr {vote_num}",
        "counts": counts,
        "named_votes": named_votes,
    }


def _parse_vote_table(table: list[list], session: SessionMeta, vote_idx: int) -> dict | None:
    if not table or len(table) < 2:
        return None
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    named_votes = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
    topic = ""
    for row in table:
        if not row:
            continue
        for cell in row:
            if not cell or not isinstance(cell, str):
                continue
            cell_clean = cell.strip()
            if not topic and len(cell_clean) > 20 and "głos" not in cell_clean.lower():
                topic = cell_clean[:300]
        if len(row) >= 2 and row[0] and row[-1]:
            name = (row[0] or "").strip()
            vote_token = (row[-1] or "").strip()
            canonical = resolve_canonical_name(name)
            if canonical:
                key = _classify_vote_token(vote_token)
                if key:
                    named_votes[key].append(canonical)
                    counts[key] = len(named_votes[key])
    if not any(named_votes.values()):
        return None
    # Session number in vote_id prevents collisions when two sessions share
    # a date. Same bug pattern as Radom 2025-03-31.
    num_part = f"_{session.number}" if getattr(session, "number", "") else ""
    return {
        "id": f"{session.date}{num_part}_{vote_idx:03d}",
        "session_number": session.number,
        "session_date": session.date,
        "topic": topic or f"Głosowanie nr {vote_idx}",
        "counts": counts,
        "named_votes": named_votes,
    }


# ---------------------------------------------------------------------------
# Build output
# ---------------------------------------------------------------------------

# Kanoniczny slugifier wspólny dla całego projektu — patrz
# radoskop/scripts/lib_slug.py (identyczne wyniki dla polskich nazwisk).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_slug import make_slug as slugify  # noqa: E402


def build_profiles(votes: list[dict]) -> list[dict]:
    profiles_by_name = {}
    for name, club in COUNCILORS.items():
        profiles_by_name[name] = {
            "name": name,
            "slug": slugify(name),
            "kadencje": {
                DEFAULT_KADENCJA: {
                    "club": club,
                    "club_full": club,
                    "frekwencja": 0,
                    "aktywnosc": 0,
                    "zgodnosc_z_klubem": 0,
                    "votes_za": 0,
                    "votes_przeciw": 0,
                    "votes_wstrzymal": 0,
                    "votes_total": 0,
                    "rebellion_count": 0,
                    "rebellions": [],
                }
            },
        }
    total_votes = len(votes)
    for v in votes:
        nv = v.get("named_votes", {})
        for key, names in nv.items():
            for n in names:
                if n not in profiles_by_name:
                    continue
                kd = profiles_by_name[n]["kadencje"][DEFAULT_KADENCJA]
                if key == "za":
                    kd["votes_za"] += 1
                elif key == "przeciw":
                    kd["votes_przeciw"] += 1
                elif key == "wstrzymal_sie":
                    kd["votes_wstrzymal"] += 1
    for p in profiles_by_name.values():
        kd = p["kadencje"][DEFAULT_KADENCJA]
        active = kd["votes_za"] + kd["votes_przeciw"] + kd["votes_wstrzymal"]
        kd["votes_total"] = active
        if total_votes > 0:
            kd["frekwencja"] = round(100 * active / total_votes, 1)
            kd["aktywnosc"] = round(100 * active / total_votes, 1)
    return list(profiles_by_name.values())


def _build_clubs_summary(councilors: list[dict]) -> dict:
    out = {}
    for c in councilors:
        club = c.get("club", "")
        if not club:
            continue
        if club not in out:
            out[club] = {"members": 0, "members_list": []}
        out[club]["members"] += 1
        out[club]["members_list"].append(c["name"])
    return out


def build_outputs(sessions: list[SessionMeta], votes: list[dict],
                  output_path: Path, profiles_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profiles_path.parent.mkdir(parents=True, exist_ok=True)

    sessions_out = []
    for s in sessions:
        s_votes = [v for v in votes if v["session_number"] == s.number and v["session_date"] == s.date]
        attendees = set()
        for v in s_votes:
            for key, names in v.get("named_votes", {}).items():
                if key in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
                    attendees.update(names)
        sessions_out.append({
            "number": s.number,
            "date": s.date,
            "url": s.detail_url,
            "vote_count": len(s_votes),
            "attendee_count": len(attendees),
            "extraordinary": s.nadzwyczajna,
        })

    profiles = build_profiles(votes)
    councilors = [
        {
            "name": p["name"],
            "slug": p["slug"],
            **p["kadencje"][DEFAULT_KADENCJA],
        }
        for p in profiles
    ]

    kad_data = {
        "id": DEFAULT_KADENCJA,
        "label": KADENCJE[DEFAULT_KADENCJA]["label"],
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": sessions_out,
        "votes": votes,
        "councilors": councilors,
        "total_sessions": len(sessions_out),
        "total_votes": len(votes),
        "total_councilors": len(councilors),
        "clubs": _build_clubs_summary(councilors),
    }

    data_index = {
        "default_kadencja": DEFAULT_KADENCJA,
        "kadencje": [{"id": DEFAULT_KADENCJA, "label": KADENCJE[DEFAULT_KADENCJA]["label"]}],
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data_index, f, ensure_ascii=False, indent=2)
    kad_path = output_path.parent / f"kadencja-{DEFAULT_KADENCJA}.json"
    with kad_path.open("w", encoding="utf-8") as f:
        json.dump(kad_data, f, ensure_ascii=False, indent=2)
    with profiles_path.open("w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)

    print(f"\n=== Wyniki ===")
    print(f"Sesji:      {len(sessions_out)}")
    print(f"Głosowań:   {len(votes)}")
    print(f"Radnych:    {len(councilors)}")
    print(f"Zapisano:   {output_path}")
    print(f"            {kad_path}")
    print(f"            {profiles_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(output_path: Path, profiles_path: Path, pdf_dir: Path, parsed_dir: Path,
           max_sessions: int = 0, debug: bool = False) -> None:
    print("\n=== Radoskop Częstochowa, BIP scraper ===")
    print(f"Output:      {output_path}")
    print(f"Profiles:    {profiles_path}")
    print(f"PDF cache:   {pdf_dir}")
    print(f"Parse cache: {parsed_dir}")

    global _NAME_LOOKUP
    _NAME_LOOKUP = build_name_lookup()
    print(f"Radnych w COUNCILORS: {len(COUNCILORS)}")

    http_session = requests.Session()

    print(f"\n[1/3] Pobieranie listy sesji z {SESSIONS_LIST_URL}")
    sessions = fetch_session_list(http_session, debug=debug, max_sessions=max_sessions)
    print(f"  Znaleziono: {len(sessions)} sesji")
    if not sessions:
        print("  UWAGA: brak sesji. BIP zmienił format? Zapisuję pusty wynik.")
        build_outputs([], [], output_path, profiles_path)
        return

    print(f"\n[2/3] Pobieranie i parsowanie PDF-ów głosowań")
    all_votes = []
    parsed_dir.mkdir(parents=True, exist_ok=True)
    for i, session in enumerate(sessions, 1):
        print(f"  [{i}/{len(sessions)}] Sesja {session.number} ({session.date})")
        try:
            pdf_url = find_voting_pdf_url(http_session, session.detail_url, debug=debug)
        except Exception as exc:
            print(f"      BŁĄD strony detalu: {exc}")
            continue
        if not pdf_url:
            print(f"      brak PDF wyników głosowań")
            continue
        pdf_path = fetch_pdf(http_session, pdf_url, pdf_dir, debug=debug)
        if not pdf_path:
            continue

        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        parsed_cache_file = parsed_dir / f"{sha}.json"
        votes: list[dict] = []
        cache_hit = False
        if parsed_cache_file.exists():
            try:
                votes = json.loads(parsed_cache_file.read_text(encoding="utf-8"))
                cache_hit = True
                if debug:
                    print(f"      [cache] {len(votes)} głosowań z parsed_votes")
            except (json.JSONDecodeError, OSError):
                votes = []

        # Pusty cache = nieudany parse z poprzedniego runu (zwykle format zmienił
        # się i parser zwracał []). Nie ufamy - re-parsujemy, może nowsza wersja
        # scrapera sobie poradzi.
        if not votes:
            votes = parse_voting_pdf(pdf_path, session, debug=debug)
            # Zapisujemy cache TYLKO jeśli parse coś zwrócił. Pusty wynik
            # zostaje bez cache'a żeby następny run mógł próbować ponownie.
            if votes:
                try:
                    parsed_cache_file.write_text(json.dumps(votes, ensure_ascii=False), encoding="utf-8")
                except OSError as exc:
                    print(f"      [warn] nie zapisałem parse cache {sha[:8]}: {exc}")
            elif not cache_hit:
                print(f"      WARN: parser zwrócił 0 głosowań z {pdf_path.name} ({pdf_path.stat().st_size}B)")

        print(f"      {len(votes)} głosowań")
        all_votes.extend(votes)

    print(f"\n[3/3] Składanie outputów")
    build_outputs(sessions, all_votes, output_path, profiles_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Radoskop Częstochowa, BIP scraper")
    parser.add_argument("--output", default="docs/data.json")
    parser.add_argument("--profiles", default="docs/profiles.json")
    parser.add_argument("--pdf-dir", default=None,
                        help="Katalog cache PDF (default: ./pdfs). Pipeline NAS przekazuje scratch dir.")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog cache HTML stron BIP (opcjonalny).")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Limit dla local dev (0 = bez limitu).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Tylko lista sesji, bez pobierania PDF.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    init_cache(args.cache_dir)
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else Path("pdfs")
    parsed_dir = pdf_dir.parent / "parsed_votes"
    output_path = Path(args.output)
    profiles_path = Path(args.profiles)

    if args.dry_run:
        global _NAME_LOOKUP
        _NAME_LOOKUP = build_name_lookup()
        sessions = fetch_session_list(requests.Session(), debug=args.debug, max_sessions=args.max_sessions)
        print(f"\nZnaleziono {len(sessions)} sesji:")
        for s in sessions:
            print(f"  {s.number:>6} | {s.date} | {s.detail_url}")
        return 0

    try:
        scrape(output_path, profiles_path, pdf_dir, parsed_dir,
               max_sessions=args.max_sessions, debug=args.debug)
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 130
    except Exception as exc:
        print(f"\nBŁĄD: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
