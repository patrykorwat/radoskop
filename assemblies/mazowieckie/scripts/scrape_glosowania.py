#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Mazowieckiego, kadencja 2024 do 2029.

BIP Mazovii pod https://bip.mazovia.pl/ udostępnia stronę z listą sesji
oraz dla każdej sesji podstronę "Wyniki głosowań" w której załączniki to
PDFy generowane przez app.esesja.pl. Każdy PDF to jedno głosowanie z
imienną listą głosów (ZA, PRZECIW, WSTRZYMUJĘ SIĘ, BRAK GŁOSU, NIEOBECNI)
oraz znacznikiem czasu.

Skrypt:
1. Pobiera listę lat z https://bip.mazovia.pl/.../vii-kadencja-2024-2029/
2. Dla każdego roku pobiera listę sesji (linki w HTML).
3. Dla każdej sesji pobiera /wyniki-glosowan.html?format=json i wyciąga
   listę PDFów z komponentu Attachment.
4. Każdy PDF pobiera, ekstrahuje tekst (pdfminer.six), parsuje topic,
   datę, listy głosów imiennych, liczby ZA/PRZECIW/itd.
5. Z danych imiennych buduje listę radnych (slot index), oblicza
   attendance per sesja (union nie-NIEOBECNI), zapisuje
   `docs/kadencja-2024-2029.json` w schemacie zgodnym z miastami.

Schemat wyjścia: id, label, sessions[], votes[], councilor_index[],
total_sessions, total_votes, total_councilors, scraped_at. Pola
`councilors[]` (statystyki per radny) i `similarity_*` zostawiamy
pustymi, bo te liczy build_metrics.py / build_profiles.py jako osobne
kroki w pipeline.

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-sessions 3
    python3 scrape_glosowania.py --output /tmp/k.json --cache-dir /tmp/maz_cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE = "https://bip.mazovia.pl"
KADENCJA_PATH = "/pl/bip/sejmik/sesje-sejmiku/vii-kadencja-2024-2029/"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "Kadencja 2024–2029"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05  # uprzejmie ale szybko, BIP Mazovia odpowiada pod 100ms

ROMAN_PATTERN = re.compile(r"\b([IVXLCDM]+)-sesja-")
DATE_PATTERN = re.compile(
    r"-(\d{1,2})-(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|"
    r"sierpnia|wrzesnia|pazdziernika|listopada|grudnia)-(\d{4})-r"
)
PL_MONTH = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "wrzesnia": 9, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}


# ---------------------------------------------------------------------------
# Pomocnicze: HTTP + cache
# ---------------------------------------------------------------------------

def fetch(url: str, *, cache_dir: Path | None = None, binary: bool = False,
          referer: str | None = None) -> bytes:
    """GET z User-Agent. Cache na dysku po hashu URL."""
    cache_path = None
    if cache_dir:
        from hashlib import md5
        suffix = ".pdf" if binary else ".bin"
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if referer:
        req.add_header("Referer", referer)
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


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", errors="replace")


def fetch_json(url: str, **kw) -> Any:
    return json.loads(fetch_text(url, **kw))


# ---------------------------------------------------------------------------
# Lista lat i sesji
# ---------------------------------------------------------------------------

def discover_year_urls(cache_dir: Path | None) -> list[str]:
    """Znajdź podstrony rocznikowe pod kadencją."""
    html = fetch_text(BASE + KADENCJA_PATH, cache_dir=cache_dir)
    pattern = rf'href="({re.escape(KADENCJA_PATH)}\d{{4}}/)"'
    found = sorted(set(re.findall(pattern, html)))
    return [BASE + p for p in found]


def discover_session_urls(year_url: str, cache_dir: Path | None) -> list[str]:
    """Linki do sesji z podstrony rocznika."""
    html = fetch_text(year_url, cache_dir=cache_dir)
    # link sesji: /pl/bip/sejmik/.../{year}/{rzymska}-sesja-{D}-{miesiac}-{YYYY}-r/
    pattern = r'href="(/pl/bip/sejmik/sesje-sejmiku/vii-kadencja-2024-2029/\d{4}/[ivxlcdm]+-sesja-[^"/]+/)"'
    found = sorted(set(re.findall(pattern, html, flags=re.IGNORECASE)))
    return [BASE + p for p in found]


def parse_session_meta(session_url: str) -> tuple[str, str]:
    """Z URL wyciągnij numer rzymski sesji i datę ISO."""
    slug = session_url.rstrip("/").split("/")[-1]
    m_num = ROMAN_PATTERN.search(slug + "-")
    number = m_num.group(1).upper() if m_num else slug.split("-")[0].upper()
    m_date = DATE_PATTERN.search(slug)
    if m_date:
        day, month_pl, year = m_date.groups()
        date_iso = f"{year}-{PL_MONTH[month_pl]:02d}-{int(day):02d}"
    else:
        date_iso = ""
    return number, date_iso


def fetch_vote_pdf_urls(session_url: str, cache_dir: Path | None) -> list[str]:
    """Z /wyniki-glosowan.html?format=json wyjmij listę URLi PDFów."""
    page_url = session_url + "wyniki-glosowan.html?format=json"
    try:
        data = fetch_json(page_url, cache_dir=cache_dir)
    except RuntimeError:
        return []
    pdfs: list[str] = []
    for comp in data.get("components", []) or []:
        if comp.get("type") != "Attachment":
            continue
        for f in comp.get("content", []) or []:
            src = f.get("src")
            if not src:
                continue
            # Filtruj tylko wyniki głosowań (czasem są też uchwały)
            if "wyniki_glosowania" in src.lower():
                pdfs.append(urljoin(BASE, src))
    return pdfs


# ---------------------------------------------------------------------------
# Parser PDF głosowania (format eSesja)
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Wyciągnij tekst z PDF używając pdfminer.six (znanego w repo)."""
    from pdfminer.high_level import extract_text
    from io import BytesIO
    return extract_text(BytesIO(pdf_bytes)) or ""


SECTION_HEADERS = ("ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ", "BRAK GŁOSU", "NIEOBECNI")
COUNTS_KEYS = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu",
    "NIEOBECNI": "nieobecni",
}


def clean_name(raw: str) -> str:
    """Usuń adnotacje i normalizuj nazwisko.

    Usuwa:
      - "(informacja zgłoszona do protokołu)" i podobne w nawiasach
      - " – informacja zgłoszona ..." (myślnik półpauza/myślnik z adnotacją)
      - "  - adnotacja" (myślnik półpauza ASCII)
    Normalizuje:
      - "Piechna- Więckiewicz" -> "Piechna-Więckiewicz" (spacja po myślniku)
      - "Piechna -Więckiewicz" -> "Piechna-Więckiewicz"
      - wielokrotne spacje do jednej
    """
    name = re.sub(r"\([^)]*\)", "", raw)
    # Tnij wszystko po em dash / en dash / dash z otaczającymi spacjami
    name = re.sub(r"\s+[–—\-]\s+.*$", "", name)
    # Spacja po/przed myślnikiem w środku wyrazu (Piechna- Więckiewicz)
    name = re.sub(r"\s*-\s*", "-", name)
    # Naprawa: jeśli "-" pożarł poprzedni separator, przywróć ", " między nazwiskami
    # Nie aplikuje się w typowym przypadku.
    name = re.sub(r"\s+", " ", name).strip(" ,;.")
    return name


def split_double_names(name: str, known: set[str]) -> list[str]:
    """Rozbij string typu 'Adam Kowalski Janina Nowak' na 2 znane nazwiska.

    Niektóre PDFy łapią 2 sąsiednie kolumny w jeden token. Jeśli całe `name`
    da się podzielić na podsekwencje słów które są znanymi nazwiskami, zwróć
    je. W przeciwnym razie zwróć `[name]`.
    """
    tokens = name.split()
    if len(tokens) < 4:
        return [name]
    # Spróbuj podzielić w każdym możliwym miejscu (po 2, 3 słowach...)
    for split_point in range(2, len(tokens) - 1):
        left = " ".join(tokens[:split_point])
        right = " ".join(tokens[split_point:])
        if left in known and right in known:
            return [left, right]
    return [name]


def parse_vote_pdf(text: str) -> dict[str, Any] | None:
    """Wyciągnij topic, czasy i listy głosów z tekstu PDF eSesja."""
    if not re.search(r"wyniki\s+g[lł]osowania", text, flags=re.IGNORECASE):
        return None

    # 1. Topic (Głosowano w sprawie: ...)
    m_topic = re.search(
        r"G[lł]osowano w sprawie:\s*(.+?)(?=\n\s*ZA:|\Z)",
        text, flags=re.DOTALL,
    )
    topic = ""
    if m_topic:
        topic = re.sub(r"\s+", " ", m_topic.group(1)).strip(" ;.")

    # 2. Linia podsumowująca: "ZA: 43 44, PRZECIW: 0, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 1, NIEOBECNI: 7 6"
    counts_line = re.search(
        r"ZA:\s*(\d+(?:\s+\d+)?)\s*,\s*"
        r"PRZECIW:\s*(\d+(?:\s+\d+)?)\s*,\s*"
        r"WSTRZYMUJ[EĘ]\s*SI[EĘ]:\s*(\d+(?:\s+\d+)?)\s*,\s*"
        r"BRAK\s*G[LŁ]OSU:\s*(\d+(?:\s+\d+)?)\s*,\s*"
        r"NIEOBECNI:\s*(\d+(?:\s+\d+)?)",
        text,
    )
    counts: dict[str, int] = {}
    if counts_line:
        for raw, key in zip(
            counts_line.groups(),
            ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"),
        ):
            # przy korekcie BIP wpisuje "43 44" — bierzemy ostatnią liczbę
            tokens = raw.split()
            counts[key] = int(tokens[-1])

    # 3. Sekcja "Wyniki imienne" (z dwukropkiem albo bez, kapitalizacja
    # nieprzewidywalna) — od tego miejsca są listy.
    m_imienne = re.search(r"Wyniki\s+imienne:?", text, flags=re.IGNORECASE)
    if not m_imienne:
        return None
    body = text[m_imienne.end():]

    # Wytnij stopkę "Głosowanie z dnia: ..." i "Wygenerowano za pomocą..."
    m_when = re.search(r"G[lł]osowanie z dnia:\s*(\d{1,2}\.\d{1,2}\.\d{4}),?\s*([\d:]+)?", body)
    voted_at = None
    voted_date = None
    if m_when:
        date_str = m_when.group(1).strip()
        time_str = (m_when.group(2) or "").strip()
        try:
            d = datetime.strptime(date_str, "%d.%m.%Y")
            voted_date = d.strftime("%Y-%m-%d")
            voted_at = voted_date
            if time_str:
                voted_at = f"{voted_at}T{time_str}"
        except ValueError:
            voted_at = date_str
        body = body[:m_when.start()]
    body = re.sub(r"Wygenerowano za pomoc.*", "", body, flags=re.DOTALL)

    # 4. Rozbij body na sekcje wg nagłówków "ZA (n)", "PRZECIW (n)", etc.
    section_re = re.compile(
        r"\n?\s*("
        r"ZA|PRZECIW|WSTRZYMUJ[EĘ]\s*SI[EĘ]|BRAK\s+G[LŁ]OSU|NIEOBECNI"
        r")\s*\(\s*\d+(?:\s+\d+)?\s*\)",
        flags=re.IGNORECASE,
    )

    matches = list(section_re.finditer(body))
    sections: dict[str, list[str]] = {k: [] for k in COUNTS_KEYS.values()}
    for i, m in enumerate(matches):
        header_raw = m.group(1).upper().replace(" ", " ")
        # normalize PL diacritics in header
        header = re.sub(r"\s+", " ", header_raw).strip()
        # find canonical key
        canon = None
        for k in SECTION_HEADERS:
            if header.startswith(k.split()[0]) and (
                k == header or k.replace(" ", "") in header.replace(" ", "")
            ):
                canon = k
                break
        if canon is None:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end]
        # Zamień nowe linie na spacje, podziel po przecinkach
        flat = re.sub(r"\s+", " ", chunk).strip()
        if not flat:
            continue
        names = [clean_name(n) for n in flat.split(",")]
        names = [n for n in names if n and not n.isdigit()]
        sections[COUNTS_KEYS[canon]] = names

    # Jeśli nie znaleźliśmy counts w linii podsumowującej, wylicz z list
    if not counts:
        counts = {k: len(v) for k, v in sections.items()}

    return {
        "topic": topic or "(brak tematu)",
        "counts": counts,
        "named_lists": sections,
        "voted_at": voted_at,
        "voted_date": voted_date,
    }


# ---------------------------------------------------------------------------
# Główny pipeline
# ---------------------------------------------------------------------------

def build_kadencja(
    *, max_sessions: int | None = None, cache_dir: Path | None = None,
    only_year: str | None = None, output_path: Path | None = None,
) -> dict[str, Any]:
    print(f"==> Pobieram listę lat z {BASE}{KADENCJA_PATH}", file=sys.stderr)
    year_urls = discover_year_urls(cache_dir)
    if only_year:
        year_urls = [u for u in year_urls if f"/{only_year}/" in u]
    print(f"    Znaleziono lat: {len(year_urls)}", file=sys.stderr)

    session_urls: list[str] = []
    for yu in year_urls:
        ses = discover_session_urls(yu, cache_dir)
        print(f"    {yu.split('/')[-2]}: {len(ses)} sesji", file=sys.stderr)
        session_urls.extend(ses)

    if max_sessions is not None:
        session_urls = session_urls[:max_sessions]

    sessions: list[dict[str, Any]] = []
    votes: list[dict[str, Any]] = []
    councilor_index: list[str] = []
    councilor_to_idx: dict[str, int] = {}
    done_session_urls: set[str] = set()

    # Wznów ze stanu jeśli output istnieje i ma sensowny shape.
    if output_path and output_path.is_file():
        try:
            with output_path.open("r", encoding="utf-8") as f:
                prev = json.load(f)
            if prev.get("id") == KADENCJA_ID and isinstance(prev.get("sessions"), list):
                sessions = prev.get("sessions", [])
                votes = prev.get("votes", [])
                councilor_index = prev.get("councilor_index", [])
                councilor_to_idx = {n: i for i, n in enumerate(councilor_index)}
                done_session_urls = {s["source_url"] for s in sessions if "source_url" in s}
                print(
                    f"    Wznawiam: {len(sessions)} sesji, {len(votes)} głosowań, "
                    f"{len(councilor_index)} radnych już w pliku.",
                    file=sys.stderr,
                )
        except (OSError, json.JSONDecodeError):
            pass

    def slot_for(name: str) -> int:
        idx = councilor_to_idx.get(name)
        if idx is None:
            idx = len(councilor_index)
            councilor_index.append(name)
            councilor_to_idx[name] = idx
        return idx

    for su in session_urls:
        if su in done_session_urls:
            continue
        number, date_iso = parse_session_meta(su)
        print(f"\n==> Sesja {number} ({date_iso}) {su}", file=sys.stderr)
        pdf_urls = fetch_vote_pdf_urls(su, cache_dir)
        print(f"    PDFów: {len(pdf_urls)}", file=sys.stderr)

        attendees: set[str] = set()
        session_vote_count = 0
        session_dates: set[str] = set()
        for vote_idx, pdf_url in enumerate(pdf_urls):
            try:
                pdf_bytes = fetch(pdf_url, cache_dir=cache_dir, binary=True,
                                  referer=su + "wyniki-glosowan.html")
            except RuntimeError as e:
                print(f"    [!] {pdf_url}: {e}", file=sys.stderr)
                continue
            try:
                text = extract_pdf_text(pdf_bytes)
            except Exception as e:
                print(f"    [!] PDF parse error {pdf_url}: {e}", file=sys.stderr)
                continue

            parsed = parse_vote_pdf(text)
            if parsed is None:
                continue

            named: dict[str, list[int]] = {k: [] for k in COUNTS_KEYS.values()}
            known_names = set(councilor_index)
            for category, names in parsed["named_lists"].items():
                for raw in names:
                    if not raw:
                        continue
                    # Jeśli dwa nazwiska sklejone w jeden token, rozbij na znane.
                    parts = split_double_names(raw, known_names)
                    for n in parts:
                        if not n:
                            continue
                        idx = slot_for(n)
                        # zarejestruj nową nazwę w `known` żeby kolejne sploty
                        # mogły ją rozdzielać w obrębie tej samej iteracji.
                        known_names.add(n)
                        named[category].append(idx)
                        if category != "nieobecni":
                            attendees.add(n)

            # Data głosowania z PDFa, fallback do URL.
            this_date = parsed.get("voted_date") or date_iso
            if this_date:
                session_dates.add(this_date)

            vote_id = f"{this_date}_{vote_idx}"
            votes.append({
                "id": vote_id,
                "session_date": this_date,
                "session_number": number,
                "source_url": pdf_url,
                "topic": parsed["topic"],
                "druk": _extract_druk(parsed["topic"]),
                "resolution": None,
                "counts": parsed["counts"],
                "named_votes": named,
                "voted_at": parsed["voted_at"],
            })
            session_vote_count += 1

        # Jeśli URL nie miał daty (np. sesja w 3 dniach), użyj najwcześniejszej
        # daty z głosowań w tej sesji.
        effective_date = date_iso or (min(session_dates) if session_dates else "")

        sessions.append({
            "date": effective_date,
            "number": number,
            "vote_count": session_vote_count,
            "attendee_count": len(attendees),
            "attendees": sorted(attendees, key=lambda s: s.lower()),
            "source_url": su,
            "speakers": [],
            "dates_in_session": sorted(session_dates) if len(session_dates) > 1 else [],
        })

        # Atomic checkpoint po każdej sesji, żeby przerwanie nie rujnowało postępu.
        if output_path:
            checkpoint = _assemble(sessions, votes, councilor_index)
            tmp = output_path.with_suffix(output_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=2)
            tmp.replace(output_path)

    return _assemble(sessions, votes, councilor_index)


def _assemble(
    sessions: list[dict[str, Any]],
    votes: list[dict[str, Any]],
    councilor_index: list[str],
) -> dict[str, Any]:
    return {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions": sessions,
        "total_sessions": len(sessions),
        "total_votes": len(votes),
        "total_councilors": len(councilor_index),
        "councilors": [],
        "votes": votes,
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilor_index,
    }


def _extract_druk(topic: str) -> str | None:
    m = re.search(r"druk\s*nr\s*([\w/\-]+)", topic, flags=re.IGNORECASE)
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "kadencja-2024-2029.json"),
    )
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--year", type=str, default=None)
    parser.add_argument(
        "--cache-dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "cache"),
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    kadencja = build_kadencja(
        max_sessions=args.max_sessions,
        cache_dir=cache_dir,
        only_year=args.year,
        output_path=output,
    )

    with output.open("w", encoding="utf-8") as f:
        json.dump(kadencja, f, ensure_ascii=False, indent=2)

    print(
        f"\nZapisano {output}: "
        f"{kadencja['total_sessions']} sesji, "
        f"{kadencja['total_votes']} głosowań, "
        f"{kadencja['total_councilors']} radnych",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
