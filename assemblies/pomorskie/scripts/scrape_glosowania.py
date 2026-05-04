#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Pomorskiego, kadencja 2024 do 2029.

BIP pomorski (bip.pomorskie.eu) używa Madkom CMS i wystawia REST API
bez autoryzacji. Inaczej niż w mazowieckim, każda sesja ma jeden zbiorczy
PDF zawierający wszystkie głosowania (po stronie na głosowanie). System
generujący PDFy: H.E.R. Systém (A.S.Partner, słowacka firma), nie eSesja,
więc parser jest dedykowany dla tego formatu.

Endpointy Madkom REST API:
  GET /api/menu/{menu_id}/articles?limit=N&offset=0&archived=0
      → lista artykułów na stronie menu
  GET /api/articles/{article_id}
      → szczegóły artykułu z attachments[]
  GET /e,pobierz,get.html?id={attachment_id}
      → pobierz plik (PDF)

Menu ID dla "Imienne wykazy głosowań / VIII kadencja" = 657
(czyli kadencja 2024-2029 w naszej numeracji; pomorski sejmik liczy
swoją kadencję jako VIII, ale my zachowujemy spójne nazewnictwo
2024-2029 we wszystkich sejmikach).

Format PDF (per strona = jedno głosowanie):
    PUNKT nr X.Y - tytuł
    Posiedzenie YYYYMMDD
    Dnia: DD.MM.YYYY HH:MM:SS
    Lp | Karta | Imię Nazwisko | DECYZJA
    LICZBA OBECNYCH/NIEOBECNYCH/RADNYCH
    ZA/PRZECIW/WSTRZYMAŁO SIĘ/NIE GŁOSOWAŁO

Decyzje: ZA, PRZECIW, WSTRZYMAŁ SIĘ, NIE GŁOSOWAŁ, NIEOBECNY.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE = "https://www.bip.pomorskie.eu"
KADENCJA_MENU_ID = 657  # "VIII kadencja" pod "Imienne wykazy głosowań"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "Kadencja 2024–2029"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05

# Mapowanie z PDF na schema głosowań (zgodne z miastami i mazowieckim).
DECYZJA_TO_KEY = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
    "WSTRZYMAL SIĘ": "wstrzymal_sie",  # bez polskich znaków, na wszelki wypadek
    "NIE GŁOSOWAŁ": "brak_glosu",
    "NIE GLOSOWAL": "brak_glosu",
    "NIEOBECNY": "nieobecni",
}


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def fetch(url: str, *, cache_dir: Path | None = None,
          binary: bool = False, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
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


def fetch_json(url: str, *, cache_dir: Path | None = None) -> Any:
    return json.loads(fetch(url, cache_dir=cache_dir, suffix=".json").decode("utf-8"))


# ---------------------------------------------------------------------------
# Discovery: lista PDFów per sesja
# ---------------------------------------------------------------------------

def discover_session_pdfs(cache_dir: Path | None) -> list[dict[str, Any]]:
    """Znajdź wszystkie attachments z menu kadencji.

    Zwraca listę dict: {id, name, url}. Każdy taki attachment to PDF
    zbiorczy dla jednej sesji.
    """
    # Krok 1: lista artykułów na stronie kadencji.
    list_url = (
        f"{BASE}/api/menu/{KADENCJA_MENU_ID}/articles"
        "?limit=50&offset=0&archived=0"
    )
    listing = fetch_json(list_url, cache_dir=cache_dir)
    # API zwraca klucz "articles" (nie "items"). Plus mainArticleId wskazuje
    # artykuł główny kategorii (zawiera attachments dla całej sekcji).
    articles = listing.get("articles") or []
    main_id = listing.get("mainArticleId")
    if main_id and not any(a.get("id") == main_id or str(a.get("id")) == str(main_id) for a in articles):
        articles.append({"id": main_id})
    if not articles:
        return []
    print(f"==> Artykułów w menu {KADENCJA_MENU_ID}: {len(articles)} "
          f"(mainArticleId={main_id})", file=sys.stderr)

    pdfs: list[dict[str, Any]] = []
    for item in articles:
        art_id = item.get("id")
        if not art_id:
            continue
        art_url = f"{BASE}/api/articles/{art_id}"
        art = fetch_json(art_url, cache_dir=cache_dir)
        attachments = art.get("attachments") or []
        for att in attachments:
            if not att.get("downloadable", True):
                continue
            ext = (att.get("extension") or "").lower()
            if ext != "pdf":
                continue
            name = att.get("name", "")
            # Filtruj tylko PDFy z imiennymi wynikami.
            if "imienn" not in name.lower():
                continue
            att_id = att.get("id")
            if att_id is None:
                continue
            pdfs.append({
                "id": att_id,
                "name": name,
                "url": f"{BASE}/e,pobierz,get.html?id={att_id}",
                "size": att.get("size"),
                "article_id": art_id,
            })
    return pdfs


# ---------------------------------------------------------------------------
# Parser PDF (H.E.R. Systém)
# ---------------------------------------------------------------------------

# "Strona: 1/7" oddziela poszczególne głosowania w PDFie.
PAGE_HEADER = re.compile(r"Strona:\s*(\d+)\s*/\s*\d+", re.IGNORECASE)

# Topic + numer punktu
TOPIC_RE = re.compile(
    r"PUNKT\s+nr\s+([\d.]+)\s*-\s*(.+?)(?=\n.*?Notatka:|\n\s*Posiedzenie\s+\d+)",
    re.DOTALL,
)
DRUK_RE = re.compile(r"DRUK\s+nr\s+([\w/\-]+)", re.IGNORECASE)
DATE_RE = re.compile(
    r"Dnia:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(\d{1,2}:\d{2}(?::\d{2})?)?"
)
SESSION_RE = re.compile(r"Posiedzenie\s+(\d{8})")  # YYYYMMDD

# Linia tabeli głosowań:
#   "{Lp}  {Karta?}  {Imię Nazwisko}  {DECYZJA}[ adnotacja]"
# Karta jest opcjonalna (pusta gdy radny nieobecny). Po decyzji może być
# adnotacja typu "GŁOSOWAŁ RĘCZNIE" — ignorujemy. Match wyłącznie do końca
# linii (nie przepuszczamy nowych wierszy w nazwisku).
ROW_RE = re.compile(
    r"^\s*(\d{1,2})\s+"  # Lp
    r"(?:(\d{1,3})\s+)?"  # Karta (opcjonalnie)
    r"([A-ZŻŚĆĘĄÓŁŃŹŻ][^\n]+?)"  # Imię Nazwisko (do końca linii, ale lazy)
    r"\s+(ZA|PRZECIW|WSTRZYMA[ŁL]\s*SI[ĘE]|NIE\s+G[ŁL]OSOWA[ŁL][AO]?|NIEOBECN[YA])"
    r"[^\n]*$",  # ewentualna adnotacja po decyzji (RĘCZNIE itp.)
    re.MULTILINE,
)

COUNT_RE = re.compile(
    r"ZA:\s*(\d+)\s*\n.*?"
    r"PRZECIW:\s*(\d+)\s*\n.*?"
    r"WSTRZYMA[ŁL]O?\s*SI[EĘ]:\s*(\d+)\s*\n.*?"
    r"NIE\s*G[ŁL]OSOWA[ŁL]O?:\s*(\d+)",
    re.DOTALL,
)


def _clean_councilor_name(raw: str) -> str:
    """Usuń adnotacje typu 'GŁOSOWAŁ RĘCZNIE', 'NIE GŁOSOWAŁA' z nazwy.

    Pomorski PDF czasem wpisuje obok imienia adnotacje że radny głosował
    ręcznie albo notatkę zmieniającą decyzję. Te musimy odsiać żeby nie
    tworzyć duplikatów w `councilor_index`.
    """
    s = re.sub(r"\s+", " ", raw).strip(" .,;:")
    # Tnij wszystko od pierwszego marker'a adnotacji (case-insensitive).
    s = re.split(
        r"\s+(?:G[ŁL]OSOWA[ŁL][AO]?|R[ĘE]CZNIE|NIE\s+G[ŁL]OSOWA|"
        r"INFORMACJA|BRAK\s+G[ŁL]OSU)\b",
        s, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    return s.strip(" .,;:")


def parse_decyzja(raw: str) -> str | None:
    """Normalizuj decyzję z PDFu na klucz schemy."""
    s = re.sub(r"\s+", " ", raw.strip().upper())
    # ASCII fallback: WSTRZYMAL SIĘ → WSTRZYMAŁ SIĘ
    if "WSTRZYM" in s:
        return "wstrzymal_sie"
    if "NIE GŁOSOWA" in s or "NIE GLOSOWA" in s:
        return "brak_glosu"
    if s == "ZA":
        return "za"
    if s == "PRZECIW":
        return "przeciw"
    if "NIEOBEC" in s:
        return "nieobecni"
    return None


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """pdftotext -layout per strona. Pdfminer nie zachowuje layoutu tabel."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, text=True, timeout=60,
        )
        text = out.stdout
    finally:
        Path(tmp).unlink(missing_ok=True)
    if not text:
        return []
    # Split po "Strona: N/M" — każda strona to oddzielne głosowanie.
    # Zachowaj header bo zawiera datę.
    pages = re.split(r"(?=Strona:\s*\d+/\d+)", text)
    return [p for p in pages if p.strip() and PAGE_HEADER.search(p)]


def parse_vote_page(page_text: str) -> dict[str, Any] | None:
    """Wyciągnij temat, datę, głosy z jednej strony PDF."""
    m_topic = TOPIC_RE.search(page_text)
    topic = ""
    if m_topic:
        punkt_nr = m_topic.group(1)
        body = re.sub(r"\s+", " ", m_topic.group(2)).strip().rstrip(":-").strip()
        topic = body
    else:
        punkt_nr = None

    m_druk = DRUK_RE.search(page_text)
    druk = m_druk.group(1) if m_druk else None

    m_date = DATE_RE.search(page_text)
    voted_date = None
    voted_at = None
    if m_date:
        d, mo, y, hms = m_date.groups()
        voted_date = f"{y}-{int(mo):02d}-{int(d):02d}"
        voted_at = voted_date
        if hms:
            voted_at = f"{voted_date}T{hms}"

    # Wyciągnij rows.
    rows: dict[str, list[str]] = {k: [] for k in
                                   ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
    for m in ROW_RE.finditer(page_text):
        lp, karta, name, decyzja_raw = m.groups()
        key = parse_decyzja(decyzja_raw)
        if key is None:
            continue
        clean = _clean_councilor_name(name)
        if not clean:
            continue
        rows[key].append(clean)

    if not any(rows.values()):
        return None

    # Counts: z linii podsumowania, fallback z len() sekcji.
    m_count = COUNT_RE.search(page_text)
    if m_count:
        counts = dict(zip(
            ("za", "przeciw", "wstrzymal_sie", "brak_glosu"),
            (int(x) for x in m_count.groups()),
        ))
        counts["nieobecni"] = len(rows["nieobecni"])
    else:
        counts = {k: len(v) for k, v in rows.items()}

    return {
        "topic": topic or "(brak tematu)",
        "punkt_nr": punkt_nr,
        "druk": druk,
        "voted_at": voted_at,
        "voted_date": voted_date,
        "counts": counts,
        "named_lists": rows,
    }


# ---------------------------------------------------------------------------
# Sesje: numer rzymski z tytułu PDFu
# ---------------------------------------------------------------------------

ROMAN_FROM_NAME = re.compile(
    r"\b(XXX[VI]?|XX[IVX]+|X[IVX]*|VIII|VII|VI|V|IV|III|II|I|\d{1,2})\s+sesj",
    re.IGNORECASE,
)


def extract_session_number(name: str) -> str:
    """Z 'Imienne wyniki głosowania z XXV sesji ...' wytnij 'XXV'."""
    m = ROMAN_FROM_NAME.search(name)
    if m:
        return m.group(1).upper()
    return ""


# ---------------------------------------------------------------------------
# Główny pipeline
# ---------------------------------------------------------------------------

def build_kadencja(
    *, cache_dir: Path | None = None,
    output_path: Path | None = None,
    max_pdfs: int | None = None,
) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    votes: list[dict[str, Any]] = []
    councilor_index: list[str] = []
    name_to_idx: dict[str, int] = {}
    done_pdf_ids: set[Any] = set()

    # Wznów z checkpointu jeśli istnieje.
    if output_path and output_path.is_file():
        try:
            prev = json.loads(output_path.read_text(encoding="utf-8"))
            if prev.get("id") == KADENCJA_ID:
                sessions = prev.get("sessions", [])
                votes = prev.get("votes", [])
                councilor_index = prev.get("councilor_index", [])
                name_to_idx = {n: i for i, n in enumerate(councilor_index)}
                done_pdf_ids = {s.get("source_pdf_id") for s in sessions
                                if s.get("source_pdf_id") is not None}
                print(f"    Wznawiam: {len(sessions)} sesji, {len(votes)} głosowań, "
                      f"{len(councilor_index)} radnych", file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            pass

    def slot_for(name: str) -> int:
        idx = name_to_idx.get(name)
        if idx is None:
            idx = len(councilor_index)
            councilor_index.append(name)
            name_to_idx[name] = idx
        return idx

    pdfs = discover_session_pdfs(cache_dir)
    print(f"    PDFów do przetworzenia: {len(pdfs)}", file=sys.stderr)
    if max_pdfs:
        pdfs = pdfs[:max_pdfs]

    for pdf_meta in pdfs:
        if pdf_meta["id"] in done_pdf_ids:
            continue
        name = pdf_meta["name"]
        session_num = extract_session_number(name) or "?"
        print(f"\n==> Sesja {session_num}: {name}", file=sys.stderr)

        try:
            pdf_bytes = fetch(pdf_meta["url"], cache_dir=cache_dir, binary=True, suffix=".pdf")
        except RuntimeError as e:
            print(f"    [!] download: {e}", file=sys.stderr)
            continue

        pages = extract_pdf_pages(pdf_bytes)
        if not pages:
            print(f"    [!] brak stron w PDF (parse error?)", file=sys.stderr)
            continue
        print(f"    Strony (głosowania): {len(pages)}", file=sys.stderr)

        attendees: set[str] = set()
        session_dates: set[str] = set()
        votes_added = 0
        for page_idx, page_text in enumerate(pages):
            parsed = parse_vote_page(page_text)
            if parsed is None:
                continue

            named: dict[str, list[int]] = {k: [] for k in
                                            ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            for category, names in parsed["named_lists"].items():
                for n in names:
                    idx = slot_for(n)
                    named[category].append(idx)
                    if category != "nieobecni":
                        attendees.add(n)

            this_date = parsed["voted_date"] or ""
            if this_date:
                session_dates.add(this_date)

            vote_id = f"{this_date}_{page_idx}"
            votes.append({
                "id": vote_id,
                "session_date": this_date,
                "session_number": session_num,
                "source_url": pdf_meta["url"],
                "source_pdf_id": pdf_meta["id"],
                "source_page": page_idx + 1,
                "topic": parsed["topic"],
                "druk": parsed["druk"],
                "punkt_nr": parsed["punkt_nr"],
                "resolution": None,
                "counts": parsed["counts"],
                "named_votes": named,
                "voted_at": parsed["voted_at"],
            })
            votes_added += 1

        effective_date = min(session_dates) if session_dates else ""
        sessions.append({
            "date": effective_date,
            "number": session_num,
            "vote_count": votes_added,
            "attendee_count": len(attendees),
            "attendees": sorted(attendees, key=lambda s: s.lower()),
            "source_pdf_id": pdf_meta["id"],
            "source_url": pdf_meta["url"],
            "speakers": [],
            "dates_in_session": sorted(session_dates) if len(session_dates) > 1 else [],
        })

        # Atomic checkpoint.
        if output_path:
            assembled = _assemble(sessions, votes, councilor_index)
            tmp = output_path.with_suffix(output_path.suffix + ".tmp")
            tmp.write_text(json.dumps(assembled, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "sessions": sorted(sessions, key=lambda s: s.get("date") or ""),
        "total_sessions": len(sessions),
        "total_votes": len(votes),
        "total_councilors": len(councilor_index),
        "councilors": [],
        "votes": sorted(votes, key=lambda v: (v.get("session_date") or "", v.get("source_page") or 0)),
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "kadencja-2024-2029.json"),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "cache_pom"),
    )
    parser.add_argument("--max-pdfs", type=int, default=None,
                        help="ograniczenie do testów (np. --max-pdfs 1)")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    kadencja = build_kadencja(
        cache_dir=cache_dir,
        output_path=output,
        max_pdfs=args.max_pdfs,
    )

    print(f"\nZapisano {output}: "
          f"{kadencja['total_sessions']} sesji, "
          f"{kadencja['total_votes']} głosowań, "
          f"{kadencja['total_councilors']} radnych", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
