#!/usr/bin/env python3
"""Scraper Mestského zastupiteľstva hlavného mesta SR Bratislavy.

Źródło: zastupitelstvo.bratislava.sk (platforma Digitálne zastupiteľstvo
od Aglo Solutions, używana przez wiele miast SK). Struktura HTML jest
przewidywalna i konsystentna.

Hierarchia danych:
1. Lista zasadnutí: kadencja landing page, linki /...-zasadnutie-DDMMYYYY/
2. Per zasadnutie: lista bodów /bod-N/
3. Per bod: tab "Hlasovanie" /?bod-typ-XXX=hlasovania
4. Per hlasowanie: 5 kategorii imiennych z linkami do profili

Mapowanie kategorii:
    "Hlasovali za"           → za
    "Hlasovali proti"        → przeciw
    "Zdržali sa hlasovania"  → wstrzymal_sie
    "Nehlasovali"            → brak_glosu
    "Neprítomní"             → nieobecni

Output: docs/kadencja-{id}.json w formacie kompatybilnym z Vilnius/Praga
(councilor_index + named_votes jako lista indeksów).

Użycie:
    python3 scrape_bratislava.py
    python3 scrape_bratislava.py --kadencja-id 2022-2026
    python3 scrape_bratislava.py --max-sessions 3   # tylko do testów
    python3 scrape_bratislava.py --skip-fetch       # użyj cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

# Playwright używany tylko dla stron hlasovania (tab content load via JS).
# Listy zasadnutí i strony bodów chodzą zwyklym urllib bo render server side.
try:
    from playwright.sync_api import sync_playwright, Browser, BrowserContext
except ImportError:
    print(
        "[bratislava] BLAD: brak playwright. Instalacja:\n"
        "  pip install playwright --break-system-packages\n"
        "  python3 -m playwright install chromium",
        file=sys.stderr,
    )
    raise


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 60
FETCH_DELAY = 0.5  # sekundy między requestami, lekka uprzejmość

CATEGORIES = ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]

# Mapowanie nagłówków kategorii. Klucze są dopasowywane case insensitive,
# wartości to nasza kategoria.
CATEGORY_HEADERS = [
    ("hlasovali za", "za"),
    ("hlasovali proti", "przeciw"),
    ("zdržali sa hlasovania", "wstrzymal_sie"),
    ("zdrzali sa hlasovania", "wstrzymal_sie"),
    ("nehlasovali", "brak_glosu"),
    ("neprítomní", "nieobecni"),
    ("nepritomni", "nieobecni"),
]

# Tytuł stopni: Mgr., Ing., MUDr., JUDr., Bc., PhD., MPH itd. usuwamy
# z nazwiska poslanca żeby uzyskać kanoniczne "Nazwisko Imię".
TITLE_TOKENS = {
    "mgr.", "ing.", "mudr.", "judr.", "bc.", "phd.", "phdr.",
    "mph", "mph,", "mph.",
    "doc.", "prof.", "art.", "arch.", "mgr",
    "et", "ph.d.", "phd",
}


# ---------------------------------------------------------------------------
# Fetch utilities
# ---------------------------------------------------------------------------


def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 3) -> str:
    """GET z retry przy URLError. Zwraca tekst."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as exc:
            last_exc = exc
            wait = 2 ** attempt
            print(f"[bratislava] fetch err ({attempt}/{retries}) {url}: {exc} — sleep {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Fetch failed after {retries} retries: {url}: {last_exc}")


def cached_fetch(url: str, cache_dir: Path, prefix: str) -> str:
    """Fetch z prostym cache na dysku. Cache jest plikiem o nazwie
    {prefix}_{slug}.html gdzie slug wyciągnięty z URL."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9]+", "_", (parsed.path + "_" + parsed.query).lower()).strip("_")[:140]
    cache_file = cache_dir / f"{prefix}_{slug}.html"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    html = fetch_url(url)
    cache_file.write_text(html, encoding="utf-8")
    time.sleep(FETCH_DELAY)
    return html


def cached_fetch_rendered(
    url: str,
    cache_dir: Path,
    prefix: str,
    context: BrowserContext,
    wait_for: str | None = None,
) -> str:
    """Jak cached_fetch ale dla stron które potrzebują JS rendering.

    Używamy Playwright. Sesja browsera przekazana z zewnątrz żeby uniknąć
    re-startu chromium per request. wait_for to opcjonalny selektor CSS
    który musi być widoczny zanim wyciagamy content (np. ".poslanci").
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9]+", "_", (parsed.path + "_" + parsed.query).lower()).strip("_")[:140]
    cache_file = cache_dir / f"{prefix}_{slug}.html"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    page = context.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=5000)
            except Exception:
                # Brak selektora to nie blocker — niektóre bod nie mają
                # hlasovania (informacyjny punkt). Zachowuj content as is.
                pass
        html = page.content()
    finally:
        page.close()

    cache_file.write_text(html, encoding="utf-8")
    time.sleep(FETCH_DELAY)
    return html


# ---------------------------------------------------------------------------
# HTML parsing helpers (regex based, no bs4 dependency)
# ---------------------------------------------------------------------------

# Link do strony zasadnutia. Pattern w landing page kadencji:
#   /mestske-zastupitelstvo-...-zasadnutie-DDMMYYYY/
# Akceptuje zarówno relatywne (/...) jak absolutne (https://...) URLe.
SESSION_LINK_RE = re.compile(
    r'href="((?:https?://[^"/]+)?/?mestske-zastupitelstvo-[^"]*-zasadnutie-(\d{2})(\d{2})(\d{4})/?)"',
    re.IGNORECASE,
)

# Link do bodu na stronie zasadnutia. /bod-N/ albo /bod-Na/ (15a).
BOD_LINK_RE = re.compile(
    r'href="((?:https?://[^"/]+)?/?mestske-zastupitelstvo-[^"]*-zasadnutie-\d{8}/bod-[^/]+/?)"',
    re.IGNORECASE,
)

# Identyfikator tabu hlasowania: ?bod-typ-XXXX=hlasovania
BOD_TYP_RE = re.compile(r"bod-typ-(\d+)", re.IGNORECASE)

# Numer hlasowania na stronie bodu
HLASOVANIE_RE = re.compile(r"Hlasovanie\s+číslo\s+(\d+)", re.IGNORECASE)

# Uznesenie MsZ N/YYYY
UZNESENIE_RE = re.compile(r"Uznesenie\s+MsZ\s+(\d+/\d+)", re.IGNORECASE)

# Pojedyncza pozycja na liście radnych w danej kategorii. Format HTML
# (po render Playwright):
#   <a href="https://.../212609-sk/antalova-plavuchova-lenka/">
#     <i class="fa fa-male"></i>Mgr. Antalová Plavuchová Lenka
#   </a>
# Nazwisko pojawia się po </i>, przed </a>. Whitespace może być dowolny.
RADNY_LINE_RE = re.compile(
    r'<a\s+href="[^"]*?/(\d+)-sk/[^"]+/"[^>]*>\s*(?:<i[^>]*>\s*</i>)?\s*([^<]+?)\s*</a>',
    re.IGNORECASE | re.DOTALL,
)

# Sekcja jednej kategorii: <div class="panel panel-theme hlasovanie-panel hlasovanie-X">
# X to za, proti, zdrzali, nehlasovali, nepritomni
HLASOVANIE_SECTION_RE = re.compile(
    r'<div\s+class="[^"]*hlasovanie-(za|proti|zdrzali|nehlasovali|nepritomni)[^"]*"[^>]*>(.*?)</div>\s*</div>\s*(?=<div\s+class="[^"]*hlasovanie-|</div>\s*</div>\s*<(?:div|section|/div))',
    re.IGNORECASE | re.DOTALL,
)

# Klasa hlasovanie-X → nasza kategoria
HLASOVANIE_CLASS_TO_CATEGORY = {
    "za": "za",
    "proti": "przeciw",
    "zdrzali": "wstrzymal_sie",
    "nehlasovali": "brak_glosu",
    "nepritomni": "nieobecni",
}

# Tytuł bodu z headera strony hlasowania
TOPIC_RE = re.compile(
    r"# Bod č\.\s*([^\n]+?)\n(.*?)\n\n\s*-\s*\[Materiály\]",
    re.DOTALL,
)


def parse_session_list(html: str, base_url: str) -> list[dict[str, str]]:
    """Z landing kadencji wyciąga listę zasadnutí.

    Zwraca [{date, url}] sortowane chronologicznie ascending.
    """
    seen = set()
    out: list[dict[str, str]] = []
    for m in SESSION_LINK_RE.finditer(html):
        path = m.group(1)
        dd, mm, yyyy = m.group(2), m.group(3), m.group(4)
        date = f"{yyyy}-{mm}-{dd}"
        url = urljoin(base_url, path)
        if url in seen:
            continue
        seen.add(url)
        out.append({"date": date, "url": url})
    out.sort(key=lambda s: s["date"])
    return out


def parse_bod_list(html: str, base_url: str) -> list[dict[str, str]]:
    """Z strony zasadnutia wyciąga listę bodów (agenda items).

    Zwraca [{number, url}] gdzie number to "0", "1", "15a" itd.
    """
    seen = set()
    out: list[dict[str, str]] = []
    for m in BOD_LINK_RE.finditer(html):
        path = m.group(1)
        url = urljoin(base_url, path)
        if url in seen:
            continue
        seen.add(url)
        # /.../bod-15a/ → "15a"
        bod_m = re.search(r"/bod-([^/]+)/", path)
        bod_num = bod_m.group(1) if bod_m else ""
        out.append({"number": bod_num, "url": url})
    return out


def find_bod_typ(html: str) -> str | None:
    """Wyciąga bod-typ identifier z linku w tabach.

    Każdy bod ma swój własny typ ID, np. ?bod-typ-328371=hlasovania.
    """
    m = BOD_TYP_RE.search(html)
    return m.group(1) if m else None


def normalize_name(raw: str) -> str:
    """Z 'Mgr. Antalová Plavuchová Lenka' → 'Antalová Plavuchová Lenka'.

    Usuwa wszystkie tokeny tytułów. Nie usuwa wewnętrznych "PhD." po nazwisku
    bo to jest częścią pełnego nazwiska radnego, ale dla matching usuwamy
    wszystkie typowe tytuły i suffix po przecinku.
    """
    # Usuń sufiks po przecinku: ", PhD." albo ", MPH"
    name = re.sub(r",\s*(PhD\.?|MPH|MBA|CSc\.?)\s*$", "", raw).strip()
    tokens = name.split()
    cleaned: list[str] = []
    for tok in tokens:
        if tok.lower().rstrip(",.") in {t.rstrip(".") for t in TITLE_TOKENS}:
            continue
        if tok.lower() in TITLE_TOKENS:
            continue
        cleaned.append(tok)
    return " ".join(cleaned).strip()


def parse_hlasovania_page(html: str, session_date: str, bod_number: str) -> list[dict[str, Any]]:
    """Z HTML tab hlasowania (Playwright rendered) wyciąga listę głosowań.

    Format ze strony Digitálne zastupiteľstvo:
      <h2>Hlasovanie číslo N</h2>
      <div class="panel hlasovanie-panel hlasovanie-za">
        ... ul.poslanci > li > a > NAME
      </div>
      <div class="panel hlasovanie-panel hlasovanie-proti">...</div>
      itd.

    Jeden bod ma najczęściej JEDNO hlasowanie, ale czasem dwa (proceduralne
    + zasadnicze). Split po nagłówku H2 "Hlasovanie číslo X".
    """
    votes: list[dict[str, Any]] = []

    # Split po "Hlasovanie číslo N" — to nagłówek H2 albo H3 albo H4
    parts = re.split(r"(?=Hlasovanie\s+číslo\s+\d+)", html)
    for part in parts[1:]:
        m_num = HLASOVANIE_RE.search(part)
        if not m_num:
            continue
        vote_num = int(m_num.group(1))

        # Uznesenie ID (opcjonalne)
        m_uzn = UZNESENIE_RE.search(part)
        uznesenie = m_uzn.group(1) if m_uzn else None

        # Wyciągnij wszystkie sekcje hlasovanie-X w obrębie tej części
        named: dict[str, list[str]] = {cat: [] for cat in CATEGORIES}

        # Prostsze podejście: dla każdej znanej klasy, znajdź <div class="hlasovanie-X..."
        # i wyciągnij content do następnej takiej klasy albo końca panel-grup.
        for cls_suffix, cat in HLASOVANIE_CLASS_TO_CATEGORY.items():
            # Znajdź wszystkie wystąpienia tej klasy w part (zwykle 1, ale może być
            # więcej jeśli są zagnieżdżenia)
            class_pattern = re.compile(
                rf'<div\s+class="[^"]*hlasovanie-{cls_suffix}(?:\s|")[^"]*"[^>]*>(.*?)</div>\s*</div>',
                re.IGNORECASE | re.DOTALL,
            )
            for m_sec in class_pattern.finditer(part):
                section = m_sec.group(1)
                # Wyciągnij wszystkie linki w sekcji
                for m_name in RADNY_LINE_RE.finditer(section):
                    raw_name = m_name.group(2).strip()
                    normalized = normalize_name(raw_name)
                    if normalized:
                        named[cat].append(normalized)

        # Jeśli brak imiennych głosów to pomijamy (np. głosowanie proceduralne
        # bez imiennej rejestracji)
        if sum(len(v) for v in named.values()) == 0:
            continue

        counts = {cat: len(named[cat]) for cat in CATEGORIES}

        vote_id = f"bratislava_{session_date}_bod{bod_number}_h{vote_num:03d}"
        votes.append({
            "id": vote_id,
            "session_date": session_date,
            "bod_number": bod_number,
            "vote_number": vote_num,
            "uznesenie": uznesenie,
            "named_votes": named,
            "counts": counts,
        })

    return votes


def parse_session_topic_for_bod(bod_html: str) -> str:
    """Z bod page wyciąga tytuł bodu (jest w breadcrumbs i jako H1)."""
    # Tytuł bodu jest w breadcrumbs jako ostatni element
    m = re.search(r"\n3\.\s+([^\n]+)\n", bod_html)
    if m:
        return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Kadencja matching
# ---------------------------------------------------------------------------


def kadencja_for_date(date_str: str, kadencje: dict[str, dict]) -> str | None:
    if not date_str:
        return None
    sorted_kad = sorted(
        kadencje.items(),
        key=lambda kv: kv[1].get("start", ""),
        reverse=True,
    )
    for kid, kdef in sorted_kad:
        start = kdef.get("start", "")
        if date_str >= start:
            return kid
    return None


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------


def scrape(
    config: dict[str, Any],
    cache_dir: Path,
    kadencja_id: str,
    context: BrowserContext,
    max_sessions: int | None = None,
    skip_fetch: bool = False,
) -> dict[str, Any]:
    """Wykonuje pełny scrape jednej kadencji.

    Zwraca dict do zapisu jako kadencja-{id}.json.
    """
    kadencje = config.get("kadencje", {})
    kdef = kadencje.get(kadencja_id, {})
    landing_url = kdef.get("session_list_url") or config.get("session_list_url", "")
    if not landing_url:
        raise RuntimeError(f"Brak session_list_url dla kadencji {kadencja_id}")

    base_url = config.get("session_list_base", "https://zastupitelstvo.bratislava.sk/")

    print(f"[bratislava] [1/3] kadencja {kadencja_id} landing", file=sys.stderr)
    # Paginate przez ?page=0, page=1, ... do pierwszej pustej.
    # Pagination jest 0-indexed (?page=0 to default, page=1 to drugi)
    sessions_all: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for pg in range(0, 20):  # bezpieczny limit, normalnie max 2-3 strony
        sep = "&" if "?" in landing_url else "?"
        page_url = f"{landing_url}{sep}page={pg}"
        landing_html = cached_fetch(page_url, cache_dir, f"landing_p{pg}")
        page_sessions = parse_session_list(landing_html, base_url)
        new_in_page = 0
        for s in page_sessions:
            if s["url"] not in seen_urls:
                sessions_all.append(s)
                seen_urls.add(s["url"])
                new_in_page += 1
        print(f"[bratislava]   page={pg}: {new_in_page} nowych sesji", file=sys.stderr)
        if new_in_page == 0:
            break
    sessions = sorted(sessions_all, key=lambda s: s["date"])
    print(f"[bratislava]   znaleziono {len(sessions)} zasadnutí", file=sys.stderr)

    # Filtruj do kadencji
    kad_start = kdef.get("start", "")
    sessions = [s for s in sessions if s["date"] >= kad_start]
    if max_sessions:
        sessions = sessions[-max_sessions:]
    print(f"[bratislava]   po filtrowaniu kadencji ({kad_start}+): {len(sessions)} sesji", file=sys.stderr)

    all_votes: list[dict[str, Any]] = []
    sessions_meta: list[dict[str, Any]] = []
    all_radni: set[str] = set()
    topic_map: dict[tuple[str, str], str] = {}  # (date, bod_number) → topic

    print("[bratislava] [2/3] pobieranie zasadnutí + bodów + hlasowaní", file=sys.stderr)
    for i, sess in enumerate(sessions, 1):
        print(f"[bratislava]   [{i}/{len(sessions)}] {sess['date']}", file=sys.stderr)
        try:
            sess_html = cached_fetch(sess["url"], cache_dir, "session")
        except Exception as exc:
            print(f"[bratislava]   WARN session fetch: {exc}", file=sys.stderr)
            continue

        bod_list = parse_bod_list(sess_html, base_url)
        session_attendees: set[str] = set()
        session_vote_count = 0

        for bod in bod_list:
            try:
                bod_html = cached_fetch(bod["url"], cache_dir, "bod")
            except Exception as exc:
                print(f"[bratislava]     WARN bod fetch {bod['number']}: {exc}", file=sys.stderr)
                continue

            bod_typ = find_bod_typ(bod_html)
            if not bod_typ:
                # Bod bez typu (np. informacyjny, bez głosowania)
                continue

            topic = parse_session_topic_for_bod(bod_html)
            topic_map[(sess["date"], bod["number"])] = topic

            hlas_url = bod["url"] + f"?bod-typ-{bod_typ}=hlasovania"
            try:
                # JS rendering wymagany: tab hlasovania ładuje się przez AJAX
                # więc plain GET zwraca tylko zakładkę Materiały. Czekamy na
                # marker "Hlasovali" w DOM albo na timeout (bod bez hlasowań).
                hlas_html = cached_fetch_rendered(
                    hlas_url,
                    cache_dir,
                    "hlas",
                    context,
                    wait_for="text=/Hlasovali za|Hlasovanie číslo/",
                )
            except Exception as exc:
                print(f"[bratislava]     WARN hlas fetch {bod['number']}: {exc}", file=sys.stderr)
                continue

            votes = parse_hlasovania_page(hlas_html, sess["date"], bod["number"])
            for v in votes:
                v["source_url"] = hlas_url
                v["topic"] = topic
                # zbierz attendees: wszyscy ktorzy są w którejkolwiek kategorii
                for cat in CATEGORIES:
                    for name in v["named_votes"].get(cat, []):
                        all_radni.add(name)
                        if cat != "nieobecni":
                            session_attendees.add(name)
                all_votes.append(v)
                session_vote_count += 1

        sessions_meta.append({
            "date": sess["date"],
            "url": sess["url"],
            "vote_count": session_vote_count,
            "attendees": sorted(session_attendees),
            "attendee_count": len(session_attendees),
        })

    # Build councilor index
    councilor_index = sorted(all_radni)
    name_to_idx = {n: i for i, n in enumerate(councilor_index)}

    # Konwersja named_votes na indeksy
    print("[bratislava] [3/3] konwersja na compact format (indeksy)", file=sys.stderr)
    votes_compact: list[dict[str, Any]] = []
    for v in all_votes:
        named_idx: dict[str, list[int]] = {cat: [] for cat in CATEGORIES}
        for cat in CATEGORIES:
            for name in v["named_votes"].get(cat, []):
                idx = name_to_idx.get(name)
                if idx is not None:
                    named_idx[cat].append(idx)
        votes_compact.append({
            "id": v["id"],
            "session_date": v["session_date"],
            "session_number": v["session_date"],  # data jako session_number bo Bratislava nie numeruje sesji
            "source_url": v.get("source_url", ""),
            "topic": v.get("topic", "")[:500],
            "druk": v.get("uznesenie") or "",
            "resolution": "",
            "counts": v["counts"],
            "named_votes": named_idx,
        })

    # Sesje w finalnym formacie
    sessions_out: list[dict[str, Any]] = []
    for s in sessions_meta:
        sessions_out.append({
            "date": s["date"],
            "number": s["date"],
            "title": s["date"],
            "source_url": s["url"],
            "vote_count": s["vote_count"],
            "attendee_count": s["attendee_count"],
            "attendees": s["attendees"],
        })
    sessions_out.sort(key=lambda s: s["date"])

    print(
        f"[bratislava] wynik: {len(sessions_out)} sesji, "
        f"{len(votes_compact)} hlasovaní, {len(councilor_index)} poslancov",
        file=sys.stderr,
    )

    return {
        "id": kadencja_id,
        "label": kdef.get("label", kadencja_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions": sessions_out,
        "votes": votes_compact,
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--kadencja-id",
        help="Konkretna kadencja do wygenerowania. Domyślnie wszystkie z config.",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=None,
        help="Limit najnowszych sesji (tylko do testów).",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Użyj cache zamiast pobierać.",
    )
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    args.cache.mkdir(parents=True, exist_ok=True)
    args.docs.mkdir(parents=True, exist_ok=True)

    kadencje_to_gen = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    # Cleanup starych kadencja-*.json poza listą
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            try:
                old.unlink()
                print(f"[bratislava] removed stale {old.name}", file=sys.stderr)
            except OSError as exc:
                print(f"[bratislava] WARN cannot remove {old.name}: {exc}", file=sys.stderr)

    # Uruchamiamy chromium raz dla całego przebiegu i reusujemy context
    # dla wszystkich kadencji + sesji + bodów. Pojedyncza inicjalizacja
    # browsera kosztuje 1-2s, każdy new_page jest tani.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="sk-SK",
        )
        try:
            for kid in kadencje_to_gen:
                out = scrape(
                    config=config,
                    cache_dir=args.cache,
                    kadencja_id=kid,
                    context=context,
                    max_sessions=args.max_sessions,
                    skip_fetch=args.skip_fetch,
                )
                if not out["votes"]:
                    print(f"[bratislava] skip kadencja-{kid}: 0 hlasovaní", file=sys.stderr)
                    continue
                out_path = args.docs / f"kadencja-{kid}.json"
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                print(
                    f"[bratislava] wrote {out_path.name}: "
                    f"{len(out['sessions'])} sesji, "
                    f"{len(out['votes'])} hlasovaní, "
                    f"{len(out['councilor_index'])} poslancov",
                    file=sys.stderr,
                )
        finally:
            context.close()
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
