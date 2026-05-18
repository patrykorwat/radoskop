#!/usr/bin/env python3
"""
Scraper głosowań Sejmiku Województwa Dolnośląskiego, VII kadencja 2024-2029.

Źródło: bip.dolnyslask.pl (Madkom CMS, React SPA + REST API).

Pipeline:
1. /api/menu/1931/articles → lista podmenu (per kadencja per rok)
2. Per podmenu /api/menu/{id}/articles → lista sesji (artykuły)
3. Per sesja /api/articles/{id} → attachments[] (PDF protokół + ZIP z PDFami głosowań)
4. Pobierz ZIP, rozpakuj — każdy PDF UCHWAŁA/DRUK w środku to JEDEN protokół
   głosowania z imienną listą głosujących.
5. Parse każdy PDF: tytuł, czas, liczby zbiorcze, imienne listy ZA/PRZECIW/
   WSTRZYMAŁO_SIĘ/Brak głosu.

Format PDF protokołu głosowania (standardowy w całym ZIP-ie):

    Sejmik Województwa Dolnośląskiego
    26 marca 2026 09:30 PROTOKÓŁ GŁOSOWANIA SESJA_XXVI
    8. Uchwała w sprawie ... DRUK XXVI/10
    Głosowanie jawne  KO , PSL+BS , PiS , Niezrzeszeni , Lewica
    26 marca 2026 15:30
    Przyjęto  (albo Odrzucono / Przyjęto jednomyślnie)
    18  ZA
    11  PRZECIW
    3   WSTRZYMAŁO SIĘ
    1   Brak głosu
    Uprawnieni:36  Suma głosów:36
    Uczestniczący:33  Głosy uczestniczących:33
    Zagłosowało:32  Oddano głosów:32
    Szczegóły
    : 18 głosów
    Piotr Kraczkowski  Katarzyna Lubiniecka-Różyło  Kamil Barczyk
    ...
    : 11 głosów   (per PRZECIW)
    ...
    : 3 głosy     (per WSTRZYMAŁO SIĘ)
    ...
    : 1 głos      (per Brak głosu)
    ...

Schemat wyjścia: id, label, sessions[], votes[], councilor_index[],
total_sessions, total_votes, total_councilors, scraped_at — zgodny z formatem
mazowieckiego scrapera.

Użycie:
    python3 scrape_glosowania.py
    python3 scrape_glosowania.py --max-sessions 1
    python3 scrape_glosowania.py --output /tmp/d.json --cache-dir /tmp/dols_cache
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE = "https://bip.dolnyslask.pl"
PROTOKOLY_MENU_ID = 1931  # "Protokoły Sesji Sejmiku" root
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024-2029)"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Radoskop/1.0 (+https://radoskop.pl)"
TIMEOUT = 30
SLEEP_BETWEEN = 0.1

PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
    # warianty bez diakrytyków na wszelki wypadek
    "wrzesnia": 9, "pazdziernika": 10,
}

DATE_RE = re.compile(
    r"(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|"
    r"wrze[sś]nia|pa[zź]dziernika|listopada|grudnia)\s+(\d{4})",
    re.IGNORECASE,
)

ROMAN_RE = re.compile(r"SESJA[\s_]+([IVXLCDM]+)")


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def fetch_bytes(url: str, *, cache_dir: Path | None = None) -> bytes:
    """Pobierz URL (z cache na dysku). Cache po SHA1 URL-a."""
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.bin"
        if cache_file.is_file():
            return cache_file.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    if cache_file:
        cache_file.write_bytes(data)
    time.sleep(SLEEP_BETWEEN)
    return data


def fetch_json(url: str, *, cache_dir: Path | None = None) -> Any:
    return json.loads(fetch_bytes(url, cache_dir=cache_dir).decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Menu/articles discovery
# ---------------------------------------------------------------------------

def list_kadencja_year_menus(cache_dir: Path | None) -> list[dict]:
    """Pobiera /api/menu/{PROTOKOLY_MENU_ID}/articles żeby znaleźć podmenu
    poszczególnych roczników VII kadencji.

    Niestety to endpoint zwraca tylko configuration columns. Lista podmenu
    jest w menu structure. Próbujemy /api/menu/{id} bezpośrednio dla każdego
    znanego ID albo używamy heurystyki na hardcoded mapping.

    HARDCODED z probe (sprawdzonych w sesji 2026-05-18):
      - menu 2738: "2024 VII kadencja"
      - menu 2752: "2025 VII kadencja"
      - menu 2850: "2026 VII kadencja"

    Jeśli sejmik dodaje 2027 — trzeba aktualizować listę albo odkryć dynamicznie
    przez /api/menu/{id}/submenu (sprawdzić jeśli endpoint istnieje).
    """
    return [
        {"menu_id": 2738, "year": 2024},
        {"menu_id": 2752, "year": 2025},
        {"menu_id": 2850, "year": 2026},
    ]


def list_session_articles(menu_id: int, cache_dir: Path | None) -> list[dict]:
    """Pobierz listę artykułów (sesji) z podmenu danego roku."""
    url = f"{BASE}/api/menu/{menu_id}/articles?limit=200&offset=0&archived=0"
    data = fetch_json(url, cache_dir=cache_dir)
    # Lista artykułów jest w polu `articles` (sprawdzony przez probe sesji)
    # Schema: `total`, `mainArticleId`, plus tabs zawiera articles
    # W mainArticleId może być pierwszy artykuł, ale lista jest w `articles[]`
    # albo w `tabs[0].articles[]`
    articles = data.get("articles", [])
    if not articles:
        # Spróbuj tabs
        for tab in data.get("tabs", []):
            articles.extend(tab.get("articles", []))
    return articles


def fetch_article(article_id: int, cache_dir: Path | None) -> dict:
    """Pobierz szczegóły artykułu + attachments."""
    url = f"{BASE}/api/articles/{article_id}"
    return fetch_json(url, cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# Parse PDF głosowania
# ---------------------------------------------------------------------------

def _parse_roman(roman: str) -> int:
    """III → 3, XXVI → 26."""
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for ch in reversed(roman.upper()):
        v = values.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total


def _load_known_councilors() -> set[str]:
    """Wczytaj listę znanych radnych z config.json (PKW source of truth).

    Lista 36 radnych Sejmiku Dolnośląskiego z PKW 2024-04-07. Używamy jej
    jako allowlist przy parsowaniu PDFów — pypdf rozbija imiona z myślnikami
    na osobne tokeny ("Calińska- Mayer"), więc samo tokenizowanie daje fałszywe
    nazwiska. Match przeciwko allowlist eliminuje ten problem.
    """
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.is_file():
        return set()
    try:
        return set(json.loads(cfg.read_text(encoding="utf-8")).get("club_assignments", {}))
    except Exception:
        return set()


_KNOWN_COUNCILORS: set[str] = set()


def _match_names_in_block(flat_text: str, expected_count: int) -> list[str]:
    """Wyciągnij znanych radnych z tekstu — fuzzy match przeciwko allowlist
    plus auto-discovery zastępców.

    Config zawiera 36 radnych z PKW 2024-04-07. Sejmik może mieć rotacje
    (rezygnacje, zastępcy spoza pierwotnej listy PKW). Wykrywamy ich
    dynamicznie: jeśli w PDF widnieje "Imię Nazwisko" TitleCase 2 słów które
    nie jest w allowlist i nie jest słowem-śmieciem (klub, header), dodajemy.

    Tolerancja na pypdf bug rozbijający "Lubiniecka-Różyło" na "Lubiniecka-" +
    "Różyło": normalizujemy "myślnik+spacja" → samego myślnika przed matchem.
    """
    global _KNOWN_COUNCILORS
    if not _KNOWN_COUNCILORS:
        _KNOWN_COUNCILORS = _load_known_councilors()
    # Polskie imiona — pierwsze słowo MUSI być z tej listy żeby auto-discovery
    # nie chwytało par typu "Nazwisko Imię" z sąsiednich pełnoprawnych radnych.
    # Lista zbudowana z config + popularne polskie imiona zastępców.
    known_first_names: set[str] = set()
    for name in _KNOWN_COUNCILORS:
        first = name.split(" ", 1)[0]
        known_first_names.add(first)
    # Plus znani spoza PKW (zastępcy obserwowani w PDF-ach VII kadencji):
    known_first_names.update({"Piotr", "Mateusz", "Katarzyna", "Joanna", "Anna",
                              "Maria", "Tomasz", "Marek", "Jan", "Paweł", "Adam",
                              "Andrzej", "Krzysztof", "Robert", "Janusz", "Wojciech",
                              "Magdalena", "Aleksandra", "Małgorzata", "Beata"})

    norm = re.sub(r"-\s+", "-", flat_text)
    norm = re.sub(r"\s+", " ", norm)
    found = []
    consumed_spans = []
    # 1. Allowlist match (sorted long→short żeby uniknąć fragmentu)
    for name in sorted(_KNOWN_COUNCILORS, key=lambda n: -len(n)):
        start = norm.find(name)
        if start >= 0:
            found.append((start, name))
            consumed_spans.append((start, start + len(name)))
    # 2. Auto-discovery zastępców: 2-słów TitleCase poza znalezionymi spans,
    #    z wymogiem że pierwsze słowo to znane imię.
    # Pattern: Imię + Nazwisko z opcjonalnymi członami po myślnikach (każdy
    # zaczyna się od wielkiej litery). Bez myślnika w pierwszym match żeby
    # nie zostawiać trailing dash typu "Lubiniecka-".
    name_part = r"[A-ZŁŚĄĘĆŃÓŻŹ][a-złśąęćńóżź]+(?:-[A-ZŁŚĄĘĆŃÓŻŹ][a-złśąęćńóżź]+)*"
    for m in re.finditer(rf"{name_part}\s+{name_part}", norm):
        s, e = m.span()
        if any(cs <= s < ce for cs, ce in consumed_spans):
            continue
        candidate = m.group(0)
        first_word = candidate.split()[0]
        if first_word not in known_first_names:
            continue  # auto-discovery TYLKO jeśli pierwsze słowo to imię
        if any(len(p.replace("-", "")) < 3 for p in candidate.split()):
            continue
        if candidate not in [f[1] for f in found]:
            found.append((s, candidate))
        consumed_spans.append((s, e))
    # Sortuj po position w tekście (zachowuje order czytania PDF)
    found.sort(key=lambda x: x[0])
    names_in_order = [n for _, n in found]
    # Cap do expected_count żeby uniknąć false positives nadwyżki
    if expected_count and len(names_in_order) > expected_count:
        names_in_order = names_in_order[:expected_count]
    return names_in_order


def parse_voting_pdf(pdf_bytes: bytes, source_url: str = "") -> dict | None:
    """Parsuj jedno PDF głosowania → dict z imiennymi wynikami.

    Format Madkom (dolnośląski sejmik): jednostronicowy PDF z headerami
    "PROTOKÓŁ GŁOSOWANIA SESJA_XXVI", tytułem uchwały z DRUK XXVI/N, czasem
    głosowania, liczbami zbiorczymi i "Szczegóły" z imiennymi listami po
    kategoriach.

    Zwraca dict lub None gdy nie udało się sparsować (np. plik nie jest
    protokołem głosowania).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        print("UWAGA: brak pypdf, zainstaluj: pip install pypdf", file=sys.stderr)
        raise
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        print(f"  PDF parse error: {exc}")
        return None
    if not reader.pages:
        return None
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if "PROTOKÓŁ GŁOSOWANIA" not in text:
        return None

    # Sesja (rzymski numer)
    session_roman = ""
    m = ROMAN_RE.search(text)
    if m:
        session_roman = m.group(1)

    # Data głosowania — pierwsza pełna data w tekście (header to czas startu sesji)
    # Ale czas konkretnego głosowania jest na linii z drugą datą + godziną
    # Strategia: weź pierwszą datę z linii zawierającej godzinę głosowania
    # (po "Głosowanie jawne ...")
    voted_at_iso = ""
    session_date_iso = ""
    # Wszystkie wystąpienia dat w PDF
    date_matches = list(DATE_RE.finditer(text))
    times = re.findall(r"\b(\d{1,2}):(\d{2})\b", text)
    if date_matches:
        d = date_matches[0]
        day, mon_word, year = int(d.group(1)), d.group(2).lower(), int(d.group(3))
        mon_word = mon_word.replace("ś", "s").replace("ź", "z")
        month = PL_MONTHS.get(mon_word, 1)
        session_date_iso = f"{year:04d}-{month:02d}-{day:02d}"
    if session_date_iso and times:
        # Bierz drugi czas (pierwszy zwykle to start sesji, drugi czas konkretnego głosowania)
        chosen = times[1] if len(times) >= 2 else times[0]
        voted_at_iso = f"{session_date_iso}T{int(chosen[0]):02d}:{int(chosen[1]):02d}:00"

    # Tytuł uchwały — między nagłówkiem "SESJA X ..." a "Głosowanie jawne"
    # Format A: "8.Uchwała w sprawie ... DRUK XXVI/10\nGłosowanie jawne ..."
    # Format B (starsze sesje): "96. Głosowanie 1 Wybór ... druk nr X/11\n..."
    topic = ""
    druk = ""
    m_topic = re.search(
        r"SESJA[\s_]+[IVXLCDM]+(?:\s+-\s+VII\s+Kadencji)?\s*\n+(.*?)\n+(?:Głosowanie jawne|Głosowanie)",
        text, re.DOTALL,
    )
    if m_topic:
        raw = m_topic.group(1).strip()
        raw = re.sub(r"^\d+\.\s*", "", raw)  # usuń "8." z początku
        # Wyciągnij DRUK XXVI/10 albo druk nr X/11 albo druk nr X_11 na końcu
        m_druk = re.search(r"(?:DRUK\s+[IVXLCDM]+[/_]\d+[a-z]?|druk\s+nr\s+[IVXLCDM]+[/_]\d+[a-z]?)", raw)
        if m_druk:
            druk = m_druk.group(0).upper().replace(" NR ", " ").replace("_", "/")
            raw = raw[: m_druk.start()].rstrip(" -—")
        topic = re.sub(r"\s+", " ", raw).strip()

    # Wynik — Przyjęto / Odrzucono / Przyjęto jednomyślnie
    resolution = ""
    for kw in ["Przyjęto jednomyślnie", "Przyjęto", "Odrzucono", "Nie podjęto", "Nie przyjęto"]:
        if kw in text:
            resolution = kw
            break

    # Liczby zbiorcze. Format dolnośląski: 4 liczby w kolejności ZA, PRZECIW,
    # WSTRZYMAŁO_SIĘ, Brak_głosu pomiędzy "Przyjęto/Odrzucono" a "Uprawnieni:".
    # Słowa "ZA/PRZECIW/WSTRZYMAŁO/Brak głosu" pojawiają się tylko jako legenda
    # PO liczbach, nie przed nimi (przeciwnie do mazowieckiego formatu).
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}

    m_block = re.search(
        r"(?:Przyjęto(?:\s+jednomyślnie)?|Odrzucono|Nie\s+przyjęto|Nie\s+podjęto)\s*\n+"
        r"(\d+)\s*\n+(\d+)\s*\n+(\d+)\s*\n+(\d+)\s*(?:Uprawnieni|\d*\s*Uprawnieni)",
        text,
    )
    if m_block:
        counts["za"] = int(m_block.group(1))
        counts["przeciw"] = int(m_block.group(2))
        counts["wstrzymal_sie"] = int(m_block.group(3))
        counts["brak_glosu"] = int(m_block.group(4))

    # Uprawnieni — nieobecni = uprawnieni - uczestniczący/obecni
    upraw = 0
    uczest = 0
    m = re.search(r"Uprawnieni\s*:\s*(\d+)", text)
    if m: upraw = int(m.group(1))
    m = re.search(r"(?:Uczestniczący|Obecni)\s*:\s*(\d+)", text)
    if m: uczest = int(m.group(1))
    if upraw and uczest:
        counts["nieobecni"] = upraw - uczest

    # Imienne listy — sekcja po "Szczegóły"
    # Format: ": 18 głosów\n<lista imion oddzielonych spacjami/newlines>\n: 11 głosów\n..."
    # Albo ": 1 głos" (singular)
    names_by_category: dict[str, list[str]] = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
    }
    # Bloki nagłówków: ": N głosów" / ": N głos" / ": N głosy" po których idą imiona
    # plus header kategorii w postaci linii ZA/PRZECIW/WSTRZYMAŁO SIĘ/Brak głosu
    # po liczbach na końcu (footer każdej kategorii)
    after = text.split("Szczegóły", 1)
    if len(after) == 2:
        details = after[1]
        # Ucinaj footer typu "1 / 1ZA\nPRZECIW\nWSTRZYMAŁO SIĘ\nBrak głosu"
        page_footer = re.search(r"\b\d+\s*/\s*\d+\s*\b", details)
        if page_footer:
            details = details[: page_footer.start()]
        category_order = []
        for key in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            if counts[key] > 0:
                category_order.append(key)

        blocks = re.split(r":\s*\d+\s+głos(?:ów|y|)?\s*", details)
        # blocks[0] = przed pierwszym blokiem (pusty albo nagłówki),
        # blocks[1..] = imiona per kategoria
        for idx, blk in enumerate(blocks[1:]):
            if idx >= len(category_order):
                break
            cat = category_order[idx]
            flat = re.sub(r"\s+", " ", blk.strip())
            # Match przeciwko allowlist 36 znanych radnych z PKW —
            # eliminuje problem pypdf rozbijającego nazwiska z myślnikami.
            names = _match_names_in_block(flat, counts.get(cat, 0))
            names_by_category[cat] = names

    return {
        "session_roman": session_roman,
        "session_date": session_date_iso,
        "voted_at": voted_at_iso,
        "topic": topic,
        "druk": druk,
        "resolution": resolution,
        "counts": counts,
        "named_votes": names_by_category,
        "source_url": source_url,
    }


# ---------------------------------------------------------------------------
# Orchestracja
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    """Zbierz unikalne nazwiska radnych ze wszystkich głosowań → posortowana lista
    + mapa name → index."""
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            for name in names:
                seen.add(name)
    sorted_names = sorted(seen)
    return sorted_names, {name: i for i, name in enumerate(sorted_names)}


def votes_to_index(vote: dict, name_to_idx: dict[str, int]) -> dict:
    """Zamień imienne listy na listy indeksów (jak w mazowieckim schema)."""
    return {
        "id": f"{vote['session_date']}_{vote.get('druk') or vote['voted_at'][-8:]}",
        "session_date": vote["session_date"],
        "session_number": vote.get("session_roman", ""),
        "source_url": vote["source_url"],
        "topic": vote["topic"],
        "druk": vote.get("druk") or None,
        "resolution": vote.get("resolution") or None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(name_to_idx[n] for n in names if n in name_to_idx)
            for cat, names in vote["named_votes"].items()
        },
        "voted_at": vote["voted_at"],
    }


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    """Z listy głosowań zbuduj listę sesji (per data)."""
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        if not d:
            continue
        sess = by_date.setdefault(d, {
            "date": d,
            "number": v.get("session_roman", ""),
            "vote_count": 0,
            "attendees": set(),
            "attendee_count": 0,
            "speakers": [],
        })
        sess["vote_count"] += 1
        # Obecność: union wszystkich kategorii oprócz "brak_glosu" + "nieobecni"
        # (brak głosu = obecny ale nie głosował, też liczy się jako obecny)
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            for name in v["named_votes"].get(cat, []):
                sess["attendees"].add(name)
    out = []
    for d in sorted(by_date.keys(), reverse=True):
        s = by_date[d]
        s["attendees"] = sorted(s["attendees"])
        s["attendee_count"] = len(s["attendees"])
        out.append(s)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Scraper Sejmiku Dolnośląskiego (BIP Madkom + PDF)")
    p.add_argument("--output", default="docs/kadencja-2024-2029.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0, help="Limit dla testu (0 = bez limitu)")
    p.add_argument("--dry-run", action="store_true", help="Tylko probe metadanych, nie pobieraj PDFów")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print(f"=== Radoskop Sejmik Dolnośląski (BIP {BASE}) ===\n")
    print("[1/4] Discover sessions per year...")

    # Lista artykułów per rok
    all_articles: list[dict] = []
    for ym in list_kadencja_year_menus(cache_dir):
        try:
            arts = list_session_articles(ym["menu_id"], cache_dir)
        except Exception as exc:
            print(f"  rok {ym['year']}: ERR {exc}")
            continue
        print(f"  rok {ym['year']}: {len(arts)} artykułów (menu {ym['menu_id']})")
        for a in arts:
            a["_year"] = ym["year"]
            all_articles.append(a)

    if args.max_sessions:
        all_articles = all_articles[: args.max_sessions]
    print(f"\n[2/4] Pobieranie szczegółów {len(all_articles)} artykułów...")

    all_votes: list[dict] = []
    for i, art in enumerate(all_articles, 1):
        aid = art.get("articleId") or art.get("id")
        if not aid:
            continue
        try:
            details = fetch_article(int(aid), cache_dir)
        except Exception as exc:
            print(f"  [{i}/{len(all_articles)}] artykuł {aid}: ERR {exc}")
            continue
        title = details.get("title", f"art-{aid}")
        attachments = details.get("attachments", []) or []
        zip_att = next((a for a in attachments if a.get("extension", "").lower() == "zip"), None)
        if not zip_att:
            print(f"  [{i}/{len(all_articles)}] {title!r}: brak ZIP attachment, pomijam")
            continue
        zip_link = zip_att.get("link", "")
        zip_url = zip_link if zip_link.startswith("http") else f"{BASE}/{zip_link}"
        print(f"  [{i}/{len(all_articles)}] {title!r}: ZIP {zip_url}")
        if args.dry_run:
            continue
        # Pobierz ZIP, rozpakuj
        try:
            zip_bytes = fetch_bytes(zip_url, cache_dir=cache_dir)
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except Exception as exc:
            print(f"    ZIP fetch/parse: {exc}")
            continue
        sess_votes_count = 0
        for name in zf.namelist():
            if not name.lower().endswith(".pdf"):
                continue
            try:
                pdf_bytes = zf.read(name)
            except Exception:
                continue
            vote = parse_voting_pdf(pdf_bytes, source_url=f"{zip_url}#{name}")
            if vote and vote["session_date"]:
                all_votes.append(vote)
                sess_votes_count += 1
        print(f"    Parsowano {sess_votes_count} głosowań z ZIP-a")

    if args.dry_run:
        print(f"\nDry-run: pominięto pobieranie PDFów. Łącznie {len(all_articles)} sesji.")
        return 0

    print(f"\n[3/4] Buduj councilor_index z {len(all_votes)} głosowań...")
    councilors, name_to_idx = build_councilor_index(all_votes)
    print(f"  Radnych unikalnych: {len(councilors)}")

    indexed_votes = [votes_to_index(v, name_to_idx) for v in all_votes]
    sessions = aggregate_sessions(all_votes)

    output = {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "total_sessions": len(sessions),
        "total_votes": len(indexed_votes),
        "total_councilors": len(councilors),
        "councilors": [],  # statystyki per radny — wypełnia build_metrics.py
        "votes": indexed_votes,
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilors,
    }

    print(f"\n[4/4] Zapisuję {args.output}...")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  {len(sessions)} sesji, {len(indexed_votes)} głosowań, {len(councilors)} radnych")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
