#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Podkarpackiego, VII kadencja 2024-2029.

Źródło: bip.podkarpackie.pl/sejmik-2/imienne-wykazy-glosowan
Struktura: HTML lista wszystkich sesji + per-sesja podstrona z PDF-ami
głosowań (jeden PDF per głosowanie, ~170 KB każdy).

Format PDF (tabela 2-kolumnowa):

    {session_seq}
    {topic_text}
    XXVII sesja Sejmiku Województwa Podkarpackiego VII kadencji
    Głosowanie {vote_number}
    Typ głosowania jawne
    Data głosowania: DD.MM.YYYY HH:MM
    Liczba uprawnionych N    Głosy za N
    Liczba obecnych N        Głosy przeciw N
    Liczba nieobecnych N     Głosy wstrzymujące się N
    Obecni niegłosujący N
    Kworum zostało osiągnięte
    Uprawnieni do głosowania
    Lp  Nazwisko i imię  Głos  Lp.  Nazwisko i imię  Głos
    1.  Bronisław Baran  ZA    18.  Mateusz Lechwar  ZA
    2.  Adam Berkowicz   ZA    19.  Czesław Łączak   WSTRZYMUJĘ SIĘ
    ...
    Wydrukowano: DD.MM.YYYY HH:MM:SS

Pipeline:
1. Crawl listingu (paginacja /sejmik-2/imienne-wykazy-glosowan?start=N) — zbierz
   linki do per-sesja podstron z URL pattern `/sejmik-2/imienne-wykazy-glosowan/{id}-{slug}`
2. Per sesja: pobierz HTML, znajdź PDFy `images/res/um/ks/VII_kadencja/{SESSION}_glosowania/`
3. Per PDF: parse tabelę, extract counts + named votes

Schema wyjścia: zgodny z mazowieckim/dolnośląskim/wielkopolskim.

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-sessions 1
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


BASE = "https://bip.podkarpackie.pl"
LISTING_ROOT = f"{BASE}/sejmik/imienne-wykazy-glosowan"
# Stary BIP (sesje I..XXVIII VII kadencji) przeniesiony na subdomenę archiwum,
# zachowuje starą strukturę URL /sejmik-2/ + PDFy w /images/res/. Nowe sesje
# (od ~XXIV) żyją na głównym BIP w CMS govarticle z załącznikami przez
# /component/govarticle?task=article.downloadAttachment.
ARCHIVE_BASE = "https://archiwumbip.podkarpackie.pl"
ARCHIVE_LISTING = f"{ARCHIVE_BASE}/sejmik-2/imienne-wykazy-glosowan"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
USER_AGENT = "Mozilla/5.0 Radoskop/1.0"
TIMEOUT = 30
SLEEP = 0.1

# SSL context — bip.podkarpackie.pl ma certyfikat który nie jest w sandbox CA bundle
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Cache + HTTP
# ---------------------------------------------------------------------------

def _ck(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def fetch_bytes(url: str, cache_dir: Path | None) -> bytes:
    cache = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"{_ck(url)}.bin"
        if cache.is_file():
            return cache.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
        data = r.read()
    if cache:
        cache.write_bytes(data)
    time.sleep(SLEEP)
    return data


def fetch_text(url: str, cache_dir: Path | None) -> str:
    return fetch_bytes(url, cache_dir).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

ROMAN_RE = re.compile(r"\b([IVXLCDM]+)\s+sesja\b", re.I)
# Slug nowego BIP: "xxix-sesja-...-vii-kadencja-z-dnia-2026-05-25"
NEW_SLUG_RE = re.compile(
    r"/sejmik/imienne-wykazy-glosowan/([ivxlcdm]+)-sesja-[^\"?]*?(?:z-dnia-(\d{4}-\d{2}-\d{2}))?$",
    re.I,
)


def discover_sessions(cache_dir: Path | None) -> list[dict]:
    """Złóż listę stron-sesji VII kadencji z dwóch źródeł:

    1. Główny BIP (CMS govarticle) — najnowsze sesje, slug bez numerycznego
       ID, np. /sejmik/imienne-wykazy-glosowan/xxix-sesja-...-z-dnia-2026-05-25
    2. Archiwum (stara struktura) — sesje I..XXVIII, slug z ID, np.
       /sejmik-2/imienne-wykazy-glosowan/7827-xxviii-sesja-...

    UWAGA: slug nowego BIP bywa błędnie podpisany (strona 'xxviii-...' serwuje
    PDFy XXVII sesji). Dlatego NIE deduplikujemy tu po numerze — zbieramy każdą
    stronę (dedup tylko po dokładnym URL), a faktyczny numer sesji i dedup
    głosowań robimy z treści PDF (patrz main()).
    """
    sessions: list[dict] = []
    seen_urls: set[str] = set()

    def add(url: str, roman: str, source: str):
        if url in seen_urls:
            return False
        seen_urls.add(url)
        sessions.append({"url": url, "roman": roman.upper(), "source": source})
        return True

    # --- Źródło 1: nowy BIP (zwykle jedna strona, brak paginacji) ---
    for start in range(0, 200, 10):
        url = LISTING_ROOT if start == 0 else f"{LISTING_ROOT}?start={start}"
        try:
            text = fetch_text(url, cache_dir)
        except Exception:
            break
        added = 0
        for path in dict.fromkeys(re.findall(
            r'href="(/sejmik/imienne-wykazy-glosowan/[a-z][^"?]*)"', text
        )):
            m = NEW_SLUG_RE.search(path)
            if not m or "vii-kadencj" not in path.lower():
                continue
            if add(f"{BASE}{path}", m.group(1), "new"):
                added += 1
        if added == 0:
            break

    # --- Źródło 2: archiwum (paginacja ?start=N co 10) ---
    for start in range(0, 300, 10):
        url = ARCHIVE_LISTING if start == 0 else f"{ARCHIVE_LISTING}?start={start}"
        try:
            text = fetch_text(url, cache_dir)
        except Exception:
            break
        added = 0
        for path, title in re.findall(
            r'href="(/sejmik-2/imienne-wykazy-glosowan/\d+-[^"]+)"[^>]*>([^<]+)</a>',
            text,
        ):
            if "VII kadencj" not in title:
                continue
            roman_m = ROMAN_RE.search(title)
            if add(f"{ARCHIVE_BASE}{path}", roman_m.group(1) if roman_m else "", "archive"):
                added += 1
        if added == 0 and start > 0:
            break

    return sessions


def discover_pdfs(session: dict, cache_dir: Path | None) -> list[str]:
    """Z per-sesja podstrony zbierz absolutne URL-e PDFów per głosowanie.

    Nowy BIP: załączniki przez /component/govarticle?task=article.downloadAttachment.
    Archiwum: statyczne PDFy w /images/res/um/ks/.
    """
    text = fetch_text(session["url"], cache_dir)
    seen: set[str] = set()
    out: list[str] = []
    if session.get("source") == "new":
        host = BASE
        raw_paths = re.findall(
            r'href="(/component/govarticle\?task=article\.downloadAttachment&(?:amp;)?id=\d+(?:&(?:amp;)?version=\d+)?)"',
            text,
        )
    else:
        host = ARCHIVE_BASE
        raw_paths = re.findall(
            r'href="((?:https?://[^"]*)?/images/res/um/ks/[^"]+\.pdf)"',
            text,
        )
    for p in raw_paths:
        p = p.replace("&amp;", "&")
        full = p if p.startswith("http") else f"{host}{p}"
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


# ---------------------------------------------------------------------------
# Parse PDF
# ---------------------------------------------------------------------------

VOTE_LABELS = ["ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ", "WSTRZYMUJE SIĘ", "NIEOBECNY", "NIEOBECNA", "BRAK GŁOSU", "OBECNY", "OBECNA"]
CAT_TO_KEY = {
    "ZA": "za", "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie",
    "NIEOBECNY": "nieobecni",
    "NIEOBECNA": "nieobecni",
    "BRAK GŁOSU": "brak_glosu",
    # "Obecni niegłosujący" w nagłówku, ale w tabeli imiennej radny obecny i
    # niegłosujący ma w kolumnie Głos token "OBECNY"/"OBECNA" (nie "BRAK GŁOSU").
    # Bez tego mapowania ci radni byli gubieni (named brak_glosu=0 vs nagłówek N).
    # Bezpieczne mimo że "OBECNY" to podłańcuch "NIEOBECNY": dopasowanie przez
    # startswith z sortowaniem longest-first, a "nieobecny" nie zaczyna się od "obecny".
    "OBECNY": "brak_glosu",
    "OBECNA": "brak_glosu",
}


def _load_known_councilors() -> set[str]:
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.is_file():
        return set()
    try:
        return set(json.loads(cfg.read_text(encoding="utf-8")).get("club_assignments", {}))
    except Exception:
        return set()


def _norm(s: str) -> str:
    """Normalizuj do dopasowania: małe litery, ł→l, bez znaków diakrytycznych.
    PDF BIP bywa niespójny (np. 'Fijolek' vs config 'Fijołek')."""
    import unicodedata
    s = s.replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


_KNOWN: set[str] = set()
_KNOWN_NORM: dict[str, str] = {}


def parse_voting_pdf(pdf_bytes: bytes, source_url: str) -> dict | None:
    """Parsuj jedno PDF głosowanie. Format Podkarpacki: 2-kolumnowa tabela
    Lp | Nazwisko Imię | Głos."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print("UWAGA: pip install pypdf", file=sys.stderr)
        raise
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return None
    if not reader.pages:
        return None
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    # Tytuł / topic
    topic = ""
    # Format: pierwszy "X. <topic>" przed "XXVII sesja Sejmiku..." lub "Głosowanie N"
    m = re.search(r"^\d+\s*\n+(.+?)(?=\n+[IVXLCDM]+\s+sesja|\n+Głosowanie\s+\d)", text, re.DOTALL)
    if m:
        topic = re.sub(r"\s+", " ", m.group(1).strip())
    else:
        # Fallback: pierwsza linia treści (po numerze)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) > 1:
            topic = lines[1][:200]

    # Numer sesji (rzymski) — WPROST z treści PDF, bo slug nowego BIP bywa
    # błędnie podpisany (np. strona 'xxviii-...' serwuje PDFy XXVII sesji).
    session_roman = ""
    m = re.search(r"([IVXLCDM]+)\s+sesja\s+Sejmiku\s+Województwa\s+Podkarpackiego", text, re.I)
    if m:
        session_roman = m.group(1).upper()

    # Data głosowania
    voted_at = ""
    session_date = ""
    m = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        d, mo, y, h, mi = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        session_date = f"{y}-{mo}-{d}"
        voted_at = f"{session_date}T{int(h):02d}:{mi}:00"

    # Counts
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    for pat, key in [
        (r"Głosy za\s+(\d+)", "za"),
        (r"Głosy przeciw\s+(\d+)", "przeciw"),
        (r"Głosy wstrzymujące się\s+(\d+)", "wstrzymal_sie"),
        (r"Obecni niegłosujący\s+(\d+)", "brak_glosu"),
        (r"Liczba nieobecnych\s+(\d+)", "nieobecni"),
    ]:
        m = re.search(pat, text)
        if m:
            counts[key] = int(m.group(1))

    # Imienne — sekcja po "Uprawnieni do głosowania"
    # Format: "Lp Nazwisko i imię Głos" header, potem rows
    names_by_cat: dict[str, list[str]] = {k: [] for k in counts}
    after = text.split("Uprawnieni do głosowania", 1)
    if len(after) != 2:
        after = text.split("Nazwisko i imię", 1)
    if len(after) == 2:
        details = after[1]
        # Ucinaj stopka "Wydrukowano:"
        details = details.split("Wydrukowano")[0]
        # Normalizuj — pypdf często wstawia \n między tokenami
        # Każdy radny: "Lp.\nImię Nazwisko\nGŁOS" lub jedna linia
        # Strategia: szukaj wzorca {Imię Nazwisko} + {GŁOS} dla każdej znanej osoby
        global _KNOWN, _KNOWN_NORM
        if not _KNOWN:
            _KNOWN = _load_known_councilors()
            _KNOWN_NORM = {_norm(n): n for n in _KNOWN}
        # Flatten + normalizuj diakrytyki (dopasowanie odporne na 'ł', NFKD)
        flat = _norm(re.sub(r"\s+", " ", details).strip())
        labels_norm = sorted(
            ((_norm(l), CAT_TO_KEY[l]) for l in VOTE_LABELS),
            key=lambda x: -len(x[0]),
        )
        # Match: każde znane "Imię Nazwisko" plus następujący GŁOS
        # (pomiń liczby Lp., dwukropki, kropki)
        for name_norm, canon in sorted(_KNOWN_NORM.items(), key=lambda kv: -len(kv[0])):
            for m in re.finditer(
                re.escape(name_norm) + r"\s+([a-z\s]+?)(?=\s+\d|\s+[a-z]+\s+[a-z]+|$)",
                flat,
            ):
                vote_text = m.group(1).strip()
                # Match na jedną z kategorii (longest first)
                matched_cat = None
                for label_norm, cat in labels_norm:
                    if vote_text.startswith(label_norm):
                        matched_cat = cat
                        break
                if matched_cat and canon not in names_by_cat[matched_cat]:
                    names_by_cat[matched_cat].append(canon)
                    break  # match per name

    return {
        "session_date": session_date,
        "voted_at": voted_at,
        "session_roman": session_roman,
        "topic": topic,
        "counts": counts,
        "named_votes": names_by_cat,
        "source_url": source_url,
    }


# ---------------------------------------------------------------------------
# Orchestracja
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            for n in names:
                seen.add(n)
    sorted_names = sorted(seen)
    return sorted_names, {n: i for i, n in enumerate(sorted_names)}


def votes_to_index(vote: dict, idx: dict[str, int], seq: int) -> dict:
    return {
        "id": f"{vote['session_date']}_{seq}",
        "session_date": vote["session_date"],
        "session_number": vote.get("session_roman", ""),
        "source_url": vote["source_url"],
        "topic": vote["topic"],
        "druk": None, "resolution": None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(idx[n] for n in names if n in idx)
            for cat, names in vote["named_votes"].items()
        },
        "voted_at": vote["voted_at"],
    }


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        if not d:
            continue
        sess = by_date.setdefault(d, {
            "date": d, "number": v.get("session_roman", ""),
            "vote_count": 0, "attendees": set(), "attendee_count": 0, "speakers": [],
        })
        sess["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for n in v["named_votes"].get(cat, []):
                sess["attendees"].add(n)
    out = []
    for d in sorted(by_date.keys(), reverse=True):
        s = by_date[d]
        s["attendees"] = sorted(s["attendees"])
        s["attendee_count"] = len(s["attendees"])
        out.append(s)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cache = Path(args.cache_dir) if args.cache_dir else None

    print(f"=== Radoskop Sejmik Podkarpacki (BIP {BASE}) ===\n")
    print("[1/3] Discover sessions...")
    sessions = discover_sessions(cache)
    print(f"  Znaleziono {len(sessions)} sesji")
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    print(f"\n[2/3] Pobieranie PDFów {len(sessions)} sesji...")
    all_votes = []
    for i, sess in enumerate(sessions, 1):
        try:
            pdfs = discover_pdfs(sess, cache)
        except Exception as exc:
            print(f"  [{i}/{len(sessions)}] {sess['roman']}: ERR {exc}")
            continue
        print(f"  [{i}/{len(sessions)}] {sess['roman']} sesja: {len(pdfs)} PDF-ów")
        if args.dry_run:
            continue
        for pdf_url in pdfs:
            try:
                pdf_bytes = fetch_bytes(pdf_url, cache)
            except Exception:
                continue
            v = parse_voting_pdf(pdf_bytes, pdf_url)
            if v and v["session_date"]:
                # Numer sesji bierzemy z treści PDF (slug bywa mylny);
                # fallback na numer ze slug/tytułu listingu.
                if not v.get("session_roman"):
                    v["session_roman"] = sess["roman"]
                all_votes.append(v)

    if args.dry_run:
        return 0

    # Dedup głosowań: ta sama uchwała trafia i do nowego BIP, i do archiwum
    # (nakładające się sesje XXIV..XXVIII). Klucz = czas głosowania + temat +
    # rozkład głosów; odporny na różne źródła/URL-e tego samego głosowania.
    deduped: list[dict] = []
    seen_keys: set = set()
    for v in all_votes:
        key = (
            v["voted_at"],
            v["session_roman"],
            re.sub(r"\s+", " ", v["topic"]).strip().lower()[:120],
            tuple(sorted(v["counts"].items())),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(v)
    if len(deduped) != len(all_votes):
        print(f"  Dedup: {len(all_votes)} → {len(deduped)} głosowań (usunięto nakładki nowy BIP/archiwum)")
    all_votes = deduped

    print(f"\n[3/3] Buduj output z {len(all_votes)} głosowań...")
    councilors, name_to_idx = build_councilor_index(all_votes)
    print(f"  Radnych unikalnych: {len(councilors)}")

    indexed = [votes_to_index(v, name_to_idx, i) for i, v in enumerate(all_votes)]
    out_sessions = aggregate_sessions(all_votes)

    output = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "sessions": out_sessions,
        "total_sessions": len(out_sessions),
        "total_votes": len(indexed),
        "total_councilors": len(councilors),
        "councilors": [], "votes": indexed,
        "similarity_top": [], "similarity_bottom": [],
        "councilor_index": councilors,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {len(out_sessions)} sesji, {len(indexed)} głosowań, {len(councilors)} radnych")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
