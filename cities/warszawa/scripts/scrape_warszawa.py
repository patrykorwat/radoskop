#!/usr/bin/env python3
"""
Scraper danych głosowań Rady Miasta Stołecznego Warszawy.

Źródło: bip.warszawa.pl/web/rada-warszawy
Portal oparty na Liferay SPA — wymaga JavaScript do renderowania treści.
Używa Playwright (headless Chromium) do pobierania stron.

Krok 1: Pobierz listę sesji (paginowana, ~10 per page)
Krok 2: Dla każdej sesji — pobierz stronę sesji i znajdź głosowania
Krok 3: Dla każdego głosowania — pobierz wyniki imienne
Krok 4: Wygeneruj data.json w formacie Radoskop

Użycie:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium
    python scrape_warszawa.py [--kadencja ix] [--output docs/data.json]

UWAGA: Uruchom lokalnie — sandbox Cowork blokuje domeny *.warszawa.pl
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Wspólne liby z radoskop/scripts (lib_stenogram) dostępne w całym module.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4 lxml")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Zainstaluj: pip install playwright && playwright install chromium")
    sys.exit(1)

BIP_BASE = "https://bip.warszawa.pl"
SESSIONS_URL = f"{BIP_BASE}/web/rada-warszawy/sesje-rady-m.st.-warszawy"
PORTLET_ID = "web_content_search_portlet_INSTANCE_fkqx"
PAGE_PARAM = f"_{PORTLET_ID}_cur"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
    "2018-2024": {"label": "VIII kadencja (2018–2024)", "start": "2018-11-22"},
}

DELAY = 0.3
STABLE_AGE_DAYS = 2
# Okno (dni od daty sesji), w którym BIP może jeszcze dorzucić docx z wynikami.
# Jeśli zapisane data.json ma sesję z 0 głosami młodszą niż to okno, skip-if
# -unchanged NIE może zadziałać — sygnatura listy sesji (number:date) się nie
# zmienia gdy docx pojawia się później, więc pusta sesja zostawała pusta na
# zawsze. Powyżej okna sesja z 0 głosami jest traktowana jak trwale pusta.
PENDING_RESULTS_WINDOW_DAYS = 30

# HTML disk cache - kluczowy speedup dla warszawa, bo każdy fetch przez
# Playwright kosztuje 3-30s (browser navigate + wait_for_networkidle + wait_for_selector).
# Cache hit zwraca BeautifulSoup z dysku BEZ ruszania Playwright.
_CACHE_DIR: Path | None = None


def init_cache(cache_dir: str | None) -> None:
    global _CACHE_DIR
    if cache_dir:
        _CACHE_DIR = Path(cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    else:
        _CACHE_DIR = None


def _cache_path(url: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    import hashlib
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.html"


def _is_session_stable(date_str: str | None) -> bool:
    """True gdy data sesji YYYY-MM-DD jest >= STABLE_AGE_DAYS dni temu."""
    if not date_str:
        return False
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return (datetime.now() - dt).days >= STABLE_AGE_DAYS


def _has_pending_empty_session(out_path: "Path") -> bool:
    """True gdy zapisane data.json ma sesję z 0 głosami w oknie publikacji wyników.

    Sesje trzymane są w kadencja-{id}.json obok data.json (index ma tylko stub'y).
    Czytamy je i szukamy sesji vote_count==0 młodszej niż PENDING_RESULTS_WINDOW
    _DAYS. Taka sesja oznacza, że docx z wynikami mógł dojść po poprzednim runie,
    więc skip-if-unchanged musi puścić pełny scrape (sygnatura listy się nie
    zmienia, bo number:date te same). Błąd odczytu => True (bezpieczniej zrobić
    pełny scrape niż utrwalić pustkę).
    """
    try:
        index = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    now = datetime.now()
    for stub in index.get("kadencje", []):
        kid = stub.get("id")
        if not kid:
            continue
        kad_path = out_path.parent / f"kadencja-{kid}.json"
        if not kad_path.exists():
            continue
        try:
            kad = json.loads(kad_path.read_text(encoding="utf-8"))
        except Exception:
            return True
        for s in kad.get("sessions", []):
            if s.get("vote_count", 0) != 0:
                continue
            date_str = s.get("date")
            if not date_str:
                continue
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if (now - dt).days <= PENDING_RESULTS_WINDOW_DAYS:
                return True
    return False

# Globalny kontekst Playwright
_browser = None
_page = None


def init_browser(headless: bool = True):
    """Uruchom przeglądarkę Playwright."""
    global _browser, _page, _pw
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=headless)
    ctx = _browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="pl-PL",
        accept_downloads=True,
    )
    _page = ctx.new_page()
    # Odrzucaj dialogi i downloady
    _page.on("download", lambda dl: dl.cancel())
    _page.on("dialog", lambda d: d.dismiss())


def close_browser():
    """Zamknij przeglądarkę."""
    global _browser, _pw
    if _browser:
        _browser.close()
    if _pw:
        _pw.stop()


_initialized = False


def ensure_bip_session():
    """Wejdź na stronę główną BIP, żeby zainicjować sesję Liferay SPA."""
    global _initialized
    if _initialized:
        return
    print("  Inicjalizacja sesji BIP...")
    _page.goto(f"{BIP_BASE}/web/rada-warszawy", wait_until="networkidle", timeout=30000)
    time.sleep(2)
    _initialized = True


def fetch(url: str, wait_for: str = "li.search-entry-list-item", save_as: str = None, use_cache: bool = True) -> BeautifulSoup:
    """Pobierz stronę przez Playwright i zwróć BeautifulSoup.

    use_cache=False wymusza świeży Playwright fetch (listy sesji, świeże sesje).
    Stabilne sesje (>2 dni) i statyczne strony używają disk cache,
    pomijając całkowicie Playwright (kluczowy speedup - browser navigate
    kosztuje 3-30s per page).
    """
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        return BeautifulSoup(cache_file.read_text(encoding="utf-8"), "lxml")

    ensure_bip_session()
    time.sleep(DELAY)
    print(f"  GET {url}")

    # Nawiguj przez JS (jak Liferay SPA) zamiast page.goto
    try:
        _page.evaluate(f"window.location.href = '{url}'")
        _page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        # Fallback na zwykłe goto
        try:
            _page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"  Uwaga przy nawigacji: {e}")
            time.sleep(3)

    # Czekaj na pojawienie się contentu
    try:
        _page.wait_for_selector(wait_for, timeout=15000)
    except Exception:
        pass
    html = _page.content()
    if save_as:
        Path(save_as).write_text(html, encoding="utf-8")
        print(f"  Zapisano HTML → {save_as}")
    if cache_file:
        try:
            cache_file.write_text(html, encoding="utf-8")
        except Exception:
            pass
    return BeautifulSoup(html, "lxml")


def scrape_session_list_all(oldest_start: str, max_pages: int = 0) -> list[dict]:
    """Pobierz paginowaną listę sesji z BIP (wszystkie kadencje od oldest_start)."""
    sessions = []
    seen_urls: set[str] = set()
    page = 1
    kadencja_start = oldest_start

    while True:
        if page == 1:
            url = SESSIONS_URL
        else:
            url = f"{SESSIONS_URL}?p_p_id={PORTLET_ID}&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view&_{PORTLET_ID}_mvcPath=%2Fview.jsp&{PAGE_PARAM}={page}"

        save = f"debug_sessions_page{page}.html" if page <= 3 else None
        # Lista sesji ZAWSZE fresh - musimy wykryć nowe sesje.
        soup = fetch(url, save_as=save, use_cache=False)
        items = soup.select("li.search-entry-list-item")

        if not items:
            print(f"  Brak elementów na stronie {page}")
            if page == 1:
                print(f"  Zapisano HTML do debug_sessions_page1.html")
            break

        new_on_page = 0
        for item in items:
            a = item.find("a", class_="search-entry-link-wrapper")
            if not a or not a.get("href"):
                continue

            href = a["href"]
            if not href.startswith("http"):
                href = BIP_BASE + href

            # Portlet wyszukiwania Liferay przy numerze strony poza zakresem
            # klampuje do ostatniej ważnej strony i zwraca te same wpisy ponownie.
            # Bez dedup po URL sesji ta sama sesja trafiała do listy 2x (i dalej
            # do głosowań, frekwencji, output). URL sesji jest unikalny per sesja,
            # więc to stabilny klucz tożsamości.
            if href in seen_urls:
                continue
            seen_urls.add(href)
            new_on_page += 1

            highlights = item.select(".search-entry-value-highlight")
            outlines = item.select(".search-entry-value-highlight-outline")

            raw_number = highlights[0].get_text(strip=True) if highlights else ""
            date = outlines[0].get_text(strip=True) if outlines else ""

            # BIP Warszawy często wstawia w highlight tytuł rubryki ("Sesja rady
            # miasta") zamiast numeru sesji — accept tylko rzymską numerację
            # albo czystą liczbę. Wszystko inne traktuj jak brak numeru;
            # pipeline dalej fallbackuje na datę.
            number = ""
            if raw_number:
                m = re.match(r"^[IVXLCDM]+$", raw_number)
                if m:
                    number = raw_number
                elif raw_number.isdigit():
                    number = raw_number
                else:
                    # Czasem highlight ma postać "LXXIII / 2024" albo "nr LX".
                    # Wyciąg pierwszy IVXLCDM token jeśli jest.
                    m2 = re.search(r"\b([IVXLCDM]{2,})\b", raw_number)
                    if m2:
                        number = m2.group(1)

            if date and date < kadencja_start:
                print(f"  Pominięto sesję {number or raw_number} ({date}) — przed {oldest_start}")
                return sessions

            sessions.append({
                "number": number,
                "date": date,
                "url": href,
            })

        # Strona bez żadnej NOWEJ sesji = Liferay zwrócił powtórkę (clamp poza
        # zakresem) albo dotarliśmy do końca. Stop, inaczej dublujemy / pętla
        # nieskończona.
        if new_on_page == 0:
            print(f"  Strona {page} bez nowych sesji — koniec paginacji")
            break

        # Paginacja — próbuj następną stronę dopóki są wyniki
        # Liferay SPA nie zawsze ma poprawny .next-btn, więc inkrementujemy i sprawdzamy
        page += 1
        if max_pages > 0 and page > max_pages:
            print(f"  (ograniczono do {max_pages} stron)")
            break

    print(f"  Znaleziono {len(sessions)} sesji")
    return sessions


_session_soup_cache: dict[str, "BeautifulSoup"] = {}


def _cached_html_has_docx(url: str) -> bool:
    """Sprawdź czy cached HTML strony sesji zawiera link do docxa głosowań.

    Pattern: `wyniki_glosowania` w href + `.docx`. Używane do invalidacji
    disk cache. BIP dorzuca docx parametrycznie 3-7 dni po sesji, czyli
    pierwszy scrape pewno tylko strony sesji bez wynikow. Bez tego sprawdz
    enia kolejne runy hitowały by stary cache na zawsze.
    """
    cache_file = _cache_path(url)
    if not cache_file or not cache_file.exists():
        return False
    try:
        cached = cache_file.read_text(encoding="utf-8")
    except Exception:
        return False
    return ("wyniki_glosowania" in cached) and (".docx" in cached.lower())


def _get_session_soup(session: dict):
    """Pobierz i zcachuj stronę sesji (unikamy podwójnego fetcha w obrębie runu).

    Plus disk cache: sesje z datą >= STABLE_AGE_DAYS hitują cache na dysku,
    pomijając całkowicie Playwright. Świeże sesje (< 2 dni) zawsze fresh.

    Cache invalidation: jeśli sesja jest stable (>= 2 dni) ale cached HTML
    nie zawiera linku `wyniki_glosowania*.docx`, force fresh fetch. BIP
    Warszawy publikuje strony sesji szybko, ale plik docx z wynikami
    dodaje 3-7 dni później. Bez tego pierwszy cache (bez docxa) zostawał
    permanentny i sesja na zawsze nie miała głosowań.
    """
    url = session["url"]
    if url not in _session_soup_cache:
        is_stable = _is_session_stable(session.get("date"))
        if is_stable and not _cached_html_has_docx(url):
            is_stable = False
        _session_soup_cache[url] = fetch(url, wait_for="article, .portlet-body", use_cache=is_stable)
    return _session_soup_cache[url]


def _normalize_href(href: str) -> str:
    if not href.startswith("http"):
        return BIP_BASE + href
    return href


def find_docx_url(session: dict) -> str | None:
    """Pobierz stronę sesji i znajdź link do wyniki_glosowania_*.docx."""
    soup = _get_session_soup(session)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "wyniki_glosowania" in href.lower() and ".docx" in href.lower():
            return _normalize_href(href)

    # Fallback: dowolny docx z "glosowania" w nazwie
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "glosowani" in href.lower() and ".docx" in href.lower():
            return _normalize_href(href)

    return None


def find_transcript_url(session: dict) -> str | None:
    """Znajdź link do transkrypcji stenogramu (preferuj DOCX wersja_tekstowa, fallback PDF).

    Liferay URLs mają format: /documents/53790/0/filename.docx/UUID
    więc sprawdzamy .docx/.pdf wewnątrz URL, nie na końcu.
    """
    soup = _get_session_soup(session)
    pdf_url = None
    docx_url = None

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        link_text = a.get_text(strip=True).lower()
        # Szukaj zarówno w href jak i w tekście linku
        is_transcript = ("transkrypcja" in href or "stenogram" in href or
                         "transkrypcja" in link_text or "stenogram" in link_text)
        if not is_transcript:
            continue
        full = _normalize_href(a["href"])
        # Liferay: .docx/.pdf może być w środku URL (nie na końcu)
        has_docx = ".docx" in href
        has_pdf = ".pdf" in href
        # Preferuj DOCX wersja_tekstowa
        if has_docx and "wersja_tekstowa" in href:
            return full
        # Następny: DOCX zanonimizowana
        if has_docx and docx_url is None:
            docx_url = full
        # PDF jako ostatni fallback
        if has_pdf and pdf_url is None:
            pdf_url = full

    return docx_url or pdf_url


def download_docx(url: str, dest: Path) -> bool:
    """Pobierz plik docx przez requests (nie wymaga JS)."""
    import requests as req
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    try:
        resp = req.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except Exception as e:
        print(f"    BŁĄD pobierania docx: {e}")
        return False


def process_session_transcript(session: dict, transcript_dir: Path) -> list[dict]:
    """Pobierz i sparsuj transkrypcję stenogramu sesji → uporządkowane tury.

    Zwraca [{"name", "text", "words"}] (pełne tury); agregat mówców liczy
    wywołujący przez lib_stenogram.aggregate_speakers.
    """
    from parse_stenogram import parse_turns

    url = find_transcript_url(session)
    if not url:
        return []

    ext = ".docx" if url.lower().endswith(".docx") else ".pdf"
    filename = f"stenogram_{session['number']}_{session['date']}{ext}"
    path = transcript_dir / filename

    if not path.exists():
        print(f"    Transkrypcja: {url.split('/')[-1][:60]}")
        if not download_docx(url, path):
            return []
    else:
        print(f"    Transkrypcja cached: {path.name}")

    try:
        turns = parse_turns(str(path))
        total_words = sum(t["words"] for t in turns)
        print(f"    Stenogram: {len(turns)} tur, {total_words} słów")
        return turns
    except Exception as e:
        print(f"    BŁĄD parsowania stenogramu: {e}")
        return []


def process_session_docx(session: dict, docx_dir: Path) -> list[dict]:
    """Pobierz docx z wynikami głosowań sesji i sparsuj."""
    from parse_wyniki_docx import parse_docx

    docx_url = find_docx_url(session)
    if not docx_url:
        print(f"    Sesja {session['number']} ({session['date']}): brak docx z wynikami")
        return []

    # Pobierz docx
    filename = f"wyniki_{session['number']}_{session['date']}.docx"
    docx_path = docx_dir / filename

    if not docx_path.exists():
        print(f"    Pobieranie: {docx_url}")
        if not download_docx(docx_url, docx_path):
            return []
    else:
        print(f"    Cached: {docx_path.name}")

    # Parsuj
    try:
        votes_raw = parse_docx(str(docx_path))
    except Exception as e:
        print(f"    BŁĄD parsowania {filename}: {e}")
        print(f"    (plik może być uszkodzony u źródła, sesja {session['date']} pominięta)")
        return []

    # Dodaj metadane sesji
    votes = []
    for vi, v in enumerate(votes_raw):
        vote_id = f"{session['date']}_{vi+1:03d}"
        votes.append({
            "id": vote_id,
            "source_url": docx_url,
            "session_date": session["date"],
            "session_number": session["number"],
            "topic": v["topic"],
            "druk": v["druk"],
            "resolution": None,
            "counts": v["counts"],
            "named_votes": v["named_votes"],
        })

    print(f"    Sesja {session['number']} ({session['date']}): {len(votes)} głosowań")
    return votes


# Kanoniczny slugifier wspólny dla całego projektu (radoskop/scripts/
# lib_slug.py): NFKD + overrides dla znaków bez dekompozycji (ł). Stare
# seedy profiles.json miały slugi z czystego NFKD, które wycinało ł
# (Paweł Lech → pawe-lech); merge_stats_to_profiles normalizuje slugi
# przy każdym runie, a stare URL-e ratuje _redirects/profiles.json.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_slug import make_slug  # noqa: E402


KLUBY_URL = "https://um.warszawa.pl/waw/radawarszawy/-/kluby-radnych-2"
# Krótkie etykiety klubów jak w dotychczasowych danych (dopasowanie po
# prefiksie pełnej nazwy ze strony, np. "Lewica – Miasto Jest Nasze –
# Wspólne Jutro" → "Lewica").
CLUB_SHORT_PREFIXES = [
    ("Koalicja Obywatelska", "KO"),
    ("Prawo i Sprawiedliwość", "PiS"),
    ("Lewica", "Lewica"),
]


def _name_key(name: str) -> frozenset:
    """Klucz dopasowania nazwiska niewrażliwy na kolejność tokenów.

    Strona klubów miesza szyk: KO/PiS listują "Nazwisko Imię", Lewica
    "Imię Nazwisko". Profile mają "Imię Nazwisko"."""
    return frozenset(t for t in re.split(r"[\s,]+", name.strip().lower()) if t)


def scrape_clubs() -> dict:
    """Aktualne przypisanie klubowe z oficjalnej strony Rady m.st. Warszawy.

    Jedyne źródło prawdy dla klubów (feedback: klub ZAWSZE wg bieżącej listy
    BIP, nie komitetu wyborczego; brak klubu = Niezrzeszeni). Strona ma
    nagłówki <strong>"Klub X"</strong> / "Niezrzeszeni" i listy <li>
    "Nazwisko Imię – funkcja w klubie".

    Zwraca {frozenset(tokeny nazwiska): {"club": short, "club_full": full}}.
    Pusta mapa przy błędzie fetch/parse — caller wtedy NIE rusza klubów.
    """
    try:
        import requests
        resp = requests.get(
            KLUBY_URL, timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Radoskop scraper)"},
        )
        resp.raise_for_status()
        html_text = resp.text
    except Exception as exc:
        print(f"  UWAGA: nie udało się pobrać strony klubów ({exc}) — kluby bez zmian")
        return {}

    soup = BeautifulSoup(html_text, "lxml")
    clubs: dict = {}
    current = None
    for el in soup.find_all(["strong", "b", "li"]):
        text = el.get_text(" ", strip=True).replace("\xa0", " ").strip()
        if not text:
            continue
        if el.name in ("strong", "b"):
            m = re.match(r"^Klub\s+(.+)$", text)
            if m:
                full = re.sub(r"\s+", " ", m.group(1)).strip()
                short = next(
                    (s for pref, s in CLUB_SHORT_PREFIXES if full.startswith(pref)),
                    full,
                )
                current = {"club": short, "club_full": full}
            elif text.lower().startswith("niezrzeszen"):
                current = {"club": "Niezrzeszeni", "club_full": None}
            continue
        if current is None:
            continue
        # <li> radnego: utnij funkcję klubową po myślniku. Pozycje spoza list
        # radnych (nawigacja przed sekcją klubów odpada przez current=None,
        # newsy/stopka za sekcją odpadają na cyfrach, pojedynczych tokenach
        # i tokenach z małej litery; resztki i tak nie zmatchują nazwisk).
        name = re.split(r"\s+[–—-]\s+", text)[0].strip()
        tokens = name.split()
        if not name or len(name) > 60 or len(tokens) < 2:
            continue
        if any(ch.isdigit() for ch in name):
            continue
        if not all(t[0].isupper() for t in tokens):
            continue
        clubs[_name_key(name)] = current
    if clubs:
        counts: dict = {}
        for v in clubs.values():
            counts[v["club"]] = counts.get(v["club"], 0) + 1
        print(f"  Kluby z BIP: {counts}")
    return clubs


def load_profiles(profiles_path: str) -> dict:
    """Wczytaj profiles.json z mapowaniem radny → klub.

    Obsługuje dwa formaty:
    - Stary: {"councilors": [{"name": ..., "club": ...}]}
    - Nowy (kompatybilny z template): {"profiles": [{"name": ..., "kadencje": {"2024-2029": {"club": ...}}}]}
    """
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path} — kluby będą oznaczone jako '?'")
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # Stary format
    if "councilors" in data:
        return {c["name"]: c for c in data["councilors"]}

    # Nowy format (template-compatible)
    result = {}
    for p in data.get("profiles", []):
        name = p["name"]
        # Znajdź dane z najnowszej kadencji
        kadencje = p.get("kadencje", {})
        if kadencje:
            latest = list(kadencje.values())[-1]
            result[name] = {
                "name": name,
                "club": latest.get("club", "?"),
                "district": latest.get("okręg"),
            }
    return result


def compute_club_majority(vote: dict, profiles: dict) -> dict[str, str]:
    """Dla każdego klubu oblicz stanowisko większości w danym głosowaniu.

    Zwraca dict: club → "za"/"przeciw"/"wstrzymal_sie"/"brak" (stanowisko większości).
    """
    club_votes = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    for cat in ["za", "przeciw", "wstrzymal_sie"]:
        for name in vote["named_votes"].get(cat, []):
            club = profiles.get(name, {}).get("club", "?")
            if club != "?":
                club_votes[club][cat] += 1

    majority = {}
    for club, counts in club_votes.items():
        best = max(counts, key=counts.get)
        majority[club] = best
    return majority


def build_councilors(all_votes: list[dict], sessions: list[dict], profiles: dict) -> list[dict]:
    """Zbuduj statystyki radnych na podstawie głosowań."""
    all_names = set()
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    councilors = {}
    for name in sorted(all_names):
        prof = profiles.get(name, {})
        councilors[name] = {
            "name": name,
            "club": prof.get("club", "?"),
            "district": prof.get("district"),
            "votes_za": 0,
            "votes_przeciw": 0,
            "votes_wstrzymal": 0,
            "votes_brak": 0,
            "votes_nieobecny": 0,
            "sessions_present": set(),
            "votes_with_club": 0,
            "votes_against_club": 0,
            "rebellions": [],
        }

    for v in all_votes:
        club_majority = compute_club_majority(v, profiles)

        for name in v["named_votes"].get("za", []):
            if name in councilors:
                councilors[name]["votes_za"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "za", club_majority, v)
        for name in v["named_votes"].get("przeciw", []):
            if name in councilors:
                councilors[name]["votes_przeciw"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "przeciw", club_majority, v)
        for name in v["named_votes"].get("wstrzymal_sie", []):
            if name in councilors:
                councilors[name]["votes_wstrzymal"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "wstrzymal_sie", club_majority, v)
        for name in v["named_votes"].get("brak_glosu", []):
            if name in councilors:
                councilors[name]["votes_brak"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("nieobecni", []):
            if name in councilors:
                councilors[name]["votes_nieobecny"] += 1

    # Only count sessions that have vote data for frekwencja calculation
    sessions_with_votes = set(v["session_date"] for v in all_votes if v.get("session_date"))
    total_sessions = len(sessions_with_votes)
    total_votes = len(all_votes)

    result = []
    for c in councilors.values():
        present_votes = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        frekwencja = (len(c["sessions_present"]) / total_sessions * 100) if total_sessions > 0 else 0
        aktywnosc = (present_votes / total_votes * 100) if total_votes > 0 else 0
        total_club_votes = c["votes_with_club"] + c["votes_against_club"]
        zgodnosc = (c["votes_with_club"] / total_club_votes * 100) if total_club_votes > 0 else 0

        result.append({
            "name": c["name"],
            "club": c["club"],
            "district": c["district"],
            "frekwencja": round(frekwencja, 1),
            "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": round(zgodnosc, 1),
            "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": len(c["rebellions"]),
            "rebellions": c["rebellions"],
        })

    return sorted(result, key=lambda x: x["name"])


def _check_rebellion(councilor: dict, vote_cat: str, club_majority: dict, vote: dict):
    """Sprawdź czy radny głosował inaczej niż większość klubu."""
    club = councilor["club"]
    if club == "?" or club not in club_majority:
        return
    majority_cat = club_majority[club]
    if vote_cat == majority_cat:
        councilor["votes_with_club"] += 1
    else:
        councilor["votes_against_club"] += 1
        councilor["rebellions"].append({
            "vote_id": vote["id"],
            "session": vote["session_date"],
            "topic": vote["topic"][:120],
            "their_vote": vote_cat,
            "club_majority": majority_cat,
        })


def compute_similarity(all_votes: list[dict], councilors_list: list[dict]) -> tuple[list, list]:
    """Oblicz pary radnych o najwyższej/najniższej zgodności głosowań."""
    from itertools import combinations

    # Zbuduj wektor głosów per radny: vote_id → kategoria
    name_to_club = {c["name"]: c["club"] for c in councilors_list}
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat

    names = sorted(vectors.keys())
    pairs = []
    for a, b in combinations(names, 2):
        # Tylko głosowania gdzie obaj brali udział (za/przeciw/wstrzymał)
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        score = round(same / len(common) * 100, 1)
        pairs.append({
            "a": a,
            "b": b,
            "club_a": name_to_club.get(a, "?"),
            "club_b": name_to_club.get(b, "?"),
            "score": score,
            "common_votes": len(common),
        })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    top = pairs[:20]
    bottom = pairs[-20:][::-1]
    return top, bottom


def build_sessions(sessions_raw: list[dict], all_votes: list[dict]) -> list[dict]:
    """Zbuduj dane sesji."""
    # Group votes by (date, session_number) to handle multiple sessions on same day
    votes_by_key: dict[tuple[str, str], list] = defaultdict(list)
    for v in all_votes:
        key = (v["session_date"], v.get("session_number", ""))
        votes_by_key[key].append(v)

    # Also keep a fallback by date only (for sessions with no matching session_number)
    votes_by_date: dict[str, list] = defaultdict(list)
    for v in all_votes:
        votes_by_date[v["session_date"]].append(v)

    # Check which dates have multiple sessions
    from collections import Counter
    date_counts = Counter(s["date"] for s in sessions_raw)

    result = []
    for s in sessions_raw:
        date = s["date"]
        number = s.get("number", "")

        if date_counts[date] > 1:
            session_votes = votes_by_key.get((date, number), [])
        else:
            session_votes = votes_by_date.get(date, [])

        attendees = set()
        for v in session_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                attendees.update(v["named_votes"].get(cat, []))

        # Fallback: per-city HTML/JS template uses session.number as the URL slug
        # in /sesja/{number}/. Empty or junk numbers break navigation, so use
        # the date as a stable slug.
        slug_number = number if number else date

        result.append({
            "date": date,
            "number": slug_number,
            "vote_count": len(session_votes),
            "attendee_count": len(attendees),
            "attendees": sorted(attendees),
        })

    return sorted(result, key=lambda x: (x["date"], x["number"]))


def compact_named_votes(output):
    """Convert named_votes from string arrays to indexed format for smaller JSON."""
    for kad in output.get("kadencje", []):
        names = set()
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat_names in nv.values():
                for n in cat_names:
                    if isinstance(n, str):
                        names.add(n)
        if not names:
            continue
        index = sorted(names, key=lambda n: n.split()[-1] + " " + n)
        name_to_idx = {n: i for i, n in enumerate(index)}
        kad["councilor_index"] = index
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat in nv:
                nv[cat] = sorted(name_to_idx[n] for n in nv[cat] if isinstance(n, str) and n in name_to_idx)
    return output



def save_split_output(output, out_path):
    """Save output as split files: data.json (index) + kadencja-{id}.json per kadencja.

    output["kadencje"] może mieć dwa typy wpisów:
      * Pełna kadencja z polem `sessions` (aktualnie scrape'owana) — zapisujemy
        plik `kadencja-{id}.json` + stub w data.json index.
      * Stub historycznej kadencji (tylko `id` i `label`, brak `sessions`) idzie
        tylko do data.json index. Plik `kadencja-{id}.json` NIE jest zapisywany,
        żeby nie nadpisać istniejącego archiwum z poprzednich scrape'ów.
    """
    import json as _json
    from pathlib import Path as _Path
    compact_named_votes(output)
    out_path = _Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        if kad.get("sessions") is None:
            continue
        kad_path = out_path.parent / f"kadencja-{kid}.json"
        with open(kad_path, "w", encoding="utf-8") as f:
            _json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": output.get("generated", ""),
        "default_kadencja": output.get("default_kadencja", ""),
        "kadencje": stubs,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


def assign_kadencja(session_date: str) -> str | None:
    """Przypisz sesję do kadencji na podstawie daty."""
    # Kadencje posortowane od najnowszej
    sorted_kads = sorted(KADENCJE.items(), key=lambda x: x[1]["start"], reverse=True)
    for kid, kinfo in sorted_kads:
        if session_date >= kinfo["start"]:
            return kid
    return None


def _write_warszawa_transcripts(kad_out: dict, session_turns: dict, out_dir, kid: str) -> int:
    """Zapisz stenogramy Warszawy do transcripts/{kid}/{num}.json + has_transcript."""
    from lib_stenogram import build_transcript, write_transcript

    club_lookup = {c["name"]: c.get("club") for c in kad_out.get("councilors", [])}
    written = 0
    for sd in kad_out.get("sessions", []):
        turns = session_turns.get(sd.get("date"))
        if not turns:
            continue
        meta = {"city": "warszawa", "city_name": "Warszawa", "kadencja": kid,
                "session_number": sd.get("number"), "date": sd.get("date")}
        if sd.get("source_url"):
            meta["source_url"] = sd["source_url"]
        tr = build_transcript(meta, turns, club_lookup)
        write_transcript(out_dir, kid, sd.get("number"), tr)
        sd["has_transcript"] = True
        sd["transcript_word_count"] = tr["stats"]["total_words"]
        written += 1
    if written:
        print(f"  Stenogramy zapisane: {written} → {out_dir}/transcripts/{kid}/")
    return written


def build_kadencja_output(kid: str, sessions: list[dict], all_votes: list[dict],
                          profiles: dict, session_speakers: dict[str, list] = None) -> dict:
    """Zbuduj output jednej kadencji."""
    kinfo = KADENCJE[kid]
    session_speakers = session_speakers or {}

    councilors = build_councilors(all_votes, sessions, profiles)
    sessions_data = build_sessions(sessions, all_votes)

    # Dodaj speakers do sesji i zbuduj activity per radny
    councilor_names = {c["name"] for c in councilors}
    councilor_activity: dict[str, dict] = {}  # name -> {sessions: [...], total_statements, total_words}

    for sd in sessions_data:
        sp = session_speakers.get(sd["date"], [])
        sd["speakers"] = sp

        # Zbierz aktywność radnych (tylko znanych radnych)
        for s in sp:
            if s["name"] not in councilor_names:
                continue
            if s["name"] not in councilor_activity:
                councilor_activity[s["name"]] = {"sessions": [], "total_statements": 0, "total_words": 0}
            act = councilor_activity[s["name"]]
            act["sessions"].append({
                "date": sd["date"],
                "session": sd.get("number", ""),
                "statements": s["statements"],
                "words": s["words"],
            })
            act["total_statements"] += s["statements"]
            act["total_words"] += s["words"]

    # Dodaj activity do councilors
    for c in councilors:
        act = councilor_activity.get(c["name"])
        if act:
            sessions_spoke = len(act["sessions"])
            c["has_activity_data"] = True
            c["activity"] = {
                "sessions_spoke": sessions_spoke,
                "total_statements": act["total_statements"],
                "total_words": act["total_words"],
                "avg_statements_per_session": round(act["total_statements"] / sessions_spoke, 1),
                "avg_words_per_session": round(act["total_words"] / sessions_spoke),
                "sessions": act["sessions"],
            }
        else:
            c["has_activity_data"] = bool(session_speakers)  # True jeśli mamy dane ale radny nie mówił
            c["activity"] = None

    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"  Kadencja {kid}: {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"    Pary podobieństwa: top={len(sim_top)}, bottom={len(sim_bottom)}")

    return {
        "id": kid,
        "label": kinfo["label"],
        "clubs": {club: count for club, count in sorted(club_counts.items())},
        "sessions": sessions_data,
        "total_sessions": len(sessions_data),
        "total_votes": len(all_votes),
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": all_votes,
        "similarity_top": sim_top,
        "similarity_bottom": sim_bottom,
    }


def merge_stats_to_profiles(profiles_path: str, output: dict, bip_clubs: dict | None = None):
    """Merge voting + activity stats from data.json councilors into profiles.json.

    Template reads profile data from profiles.json, so stats must be there.
    Dodatkowo: normalizuje slugi (stare seedy gubiły ł) i nadpisuje klub
    bieżącej kadencji wg oficjalnej listy BIP (bip_clubs ze scrape_clubs).
    """
    path = Path(profiles_path)
    if not path.exists():
        print("  Pominięto merge — brak profiles.json")
        return

    with open(path, encoding="utf-8") as f:
        profiles = json.load(f)

    current_kid = output.get("default_kadencja")

    # Build lookup: (kadencja_id, name) -> councilor stats.
    # .get: stuby historycznych kadencji i split data.json index nie mają
    # councilors — wtedy merge tylko normalizuje slugi i kluby.
    stats: dict[tuple[str, str], dict] = {}
    for kad in output["kadencje"]:
        kid = kad["id"]
        for c in kad.get("councilors", []):
            stats[(kid, c["name"])] = c

    updated = 0
    # Build reverse lookup: name -> set of kadencja IDs they appear in
    name_kadencje: dict[str, set[str]] = {}
    for (kid, name) in stats:
        name_kadencje.setdefault(name, set()).add(kid)

    for p in profiles.get("profiles", []):
        # Normalizacja sluga: stare seedy używały NFKD bez transliteracji,
        # które wycina ł (Paweł Lech → pawe-lech). Stare URL-e ratuje
        # redirect map _redirects/profiles.json (generate_seo_pages.py).
        _good_slug = make_slug(p.get("name", ""))
        if _good_slug and p.get("slug") != _good_slug:
            print(f"  Slug: {p.get('slug')} -> {_good_slug}")
            p["slug"] = _good_slug

        if "kadencje" not in p:
            p["kadencje"] = {}
        # Add missing kadencje for this person
        for kid in name_kadencje.get(p["name"], set()):
            if kid not in p["kadencje"]:
                p["kadencje"][kid] = {}

        # Klub bieżącej kadencji ZAWSZE wg oficjalnej listy BIP. Kadencje
        # historyczne zostają nietknięte (przypisanie z epoki).
        if bip_clubs and current_kid and current_kid in p["kadencje"]:
            _hit = bip_clubs.get(_name_key(p["name"]))
            if _hit:
                _entry = p["kadencje"][current_kid]
                if _entry.get("club") != _hit["club"]:
                    _entry["club"] = _hit["club"]
                if _hit["club_full"]:
                    _entry["club_full"] = _hit["club_full"]
                elif "club_full" in _entry:
                    del _entry["club_full"]

        for kid, entry in p["kadencje"].items():
            c = stats.get((kid, p["name"]))
            if not c:
                continue
            # Merge voting stats
            for key in ["frekwencja", "aktywnosc", "zgodnosc_z_klubem",
                        "votes_za", "votes_przeciw", "votes_wstrzymal",
                        "votes_brak", "votes_nieobecny", "votes_total",
                        "rebellion_count", "rebellions",
                        "sessions_attended", "attendance_count"]:
                if key in c:
                    entry[key] = c[key]
            if not entry.get("club") and c.get("club"):
                entry["club"] = c["club"]
            entry["has_voting_data"] = True
            # Merge activity stats
            entry["has_activity_data"] = c.get("has_activity_data", False)
            if c.get("activity"):
                entry["activity"] = c["activity"]
            elif "activity" in entry:
                del entry["activity"]
            updated += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"  Zaktualizowano profiles.json: {updated} wpisów")


def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Warszawy (BIP)")
    parser.add_argument("--kadencja", default="2024-2029",
                        help="ID kadencji (2024-2029, 2018-2024) lub 'all' dla wszystkich")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=0.3, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji do pobrania (0=wszystkie)")
    parser.add_argument("--max-pages", type=int, default=0, help="Maks. stron paginacji (0=wszystkie)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--explore", action="store_true", help="Pobierz 1 sesję i pokaż strukturę HTML")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json z klubami")
    parser.add_argument("--headed", action="store_true", help="Pokaż okno przeglądarki (debug)")
    parser.add_argument("--only-transcripts", action="store_true",
                        help="Tylko transkrypcje — pomiń głosowania, użyj istniejącego data.json")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog HTML cache. Sesje >2 dni hitują dysk pomijając Playwright. "
                             "Pipeline scrape_all.sh przekazuje scratch_dir/.cache/html.")
    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    init_cache(args.cache_dir)
    if args.cache_dir:
        print(f"Cache HTML: {args.cache_dir}")

    # Ustal które kadencje scrapować
    if args.kadencja == "all":
        target_kadencje = list(KADENCJE.keys())
        oldest_start = min(k["start"] for k in KADENCJE.values())
    else:
        target_kadencje = [args.kadencja]
        oldest_start = KADENCJE.get(args.kadencja, {}).get("start", "2024-01-01")

    print(f"=== Radoskop Scraper: Rada m.st. Warszawy (BIP) ===")
    print(f"Kadencje: {', '.join(target_kadencje)}")
    print(f"Backend: Playwright (headless={'nie' if args.headed else 'tak'})")
    print()

    # Timing log: warszawa to bottleneck pipeline'u, breakdown po fazach pomaga
    # zlokalizować gdzie idą sekundy (Playwright init / lista sesji / per-sesja
    # / parsing / build output).
    _t0 = time.time()
    _timings: list[tuple[str, float]] = []
    def _mark(name: str) -> None:
        _timings.append((name, time.time() - _t0))

    init_browser(headless=not args.headed)
    _mark("init_browser")

    try:
        # 1. Lista sesji — pobierz wszystkie strony, potem filtruj po kadencji
        print("[1/4] Pobieranie listy sesji...")
        # Tymczasowo ustaw najstarszą kadencję żeby pobrać wszystkie sesje
        all_sessions = scrape_session_list_all(oldest_start, max_pages=args.max_pages)
        _mark("scrape_session_list")

        # Skip-if-unchanged: jeśli signature listy sesji nie różni się od
        # poprzedniego runu I istnieje docs/data.json - exit early. Zaoszczędza
        # ~95% czasu warszawy gdy nic się nie zmieniło między runami.
        if all_sessions and _CACHE_DIR is not None and not args.only_transcripts:
            sig_path = _CACHE_DIR.parent / "session_signature.json"
            import hashlib as _h
            sig_input = "|".join(
                sorted(f"{s.get('number','')}:{s.get('date','')}" for s in all_sessions)
            )
            current_sig = _h.sha256(sig_input.encode("utf-8")).hexdigest()
            out_path_pre = Path(args.output)
            try:
                if sig_path.exists() and out_path_pre.exists():
                    stored = json.loads(sig_path.read_text(encoding="utf-8"))
                    # sig_v2 wymusza pełny scrape po dodaniu historical_stubs do
                    # data.json (stare runy bez stub'ów miały tylko aktywną
                    # kadencję, SPA nie znajdowało starych głosowań).
                    if (
                        stored.get("sig_v2") == current_sig
                        and stored.get("kadencje") == target_kadencje
                        and stored.get("kadencje_known") == sorted(KADENCJE.keys())
                    ):
                        # Sygnatura listy = number:date. Nie wykrywa pojawienia
                        # się docx z wynikami dla już znanej sesji. Jeśli zapisane
                        # data.json ma pustą sesję w oknie publikacji wyników, nie
                        # skipuj — inaczej pusta sesja zostaje pusta na zawsze.
                        if _has_pending_empty_session(out_path_pre):
                            print("\n[skip-check] Signature matched, ale jest sesja z 0 głosami w oknie publikacji wyników — pełny scrape żeby dociągnąć docx.")
                        else:
                            elapsed = time.time() - _t0
                            print(f"\n[skip] Lista sesji identyczna z poprzednim runem ({len(all_sessions)} sesji, signature matched). data.json zachowane.")
                            # Kluby i slugi odświeżamy też przy skipie —
                            # zmiana przynależności klubowej nie tworzy
                            # nowej sesji, więc bez tego czekałaby na
                            # następny pełny scrape. data.json z dysku
                            # służy tylko za index kadencji (merge bez
                            # statystyk normalizuje slugi i kluby).
                            try:
                                with open(out_path_pre, encoding="utf-8") as f:
                                    _existing_out = json.load(f)
                                merge_stats_to_profiles(
                                    args.profiles, _existing_out, scrape_clubs()
                                )
                            except Exception as exc:
                                print(f"  [skip] refresh profili nieudany: {exc}")
                            print(f"=== Timing ===\n  scrape_session_list: {elapsed:.1f}s (cumulative, skipped reszta)")
                            return
            except Exception as exc:
                print(f"  [skip-check] błąd: {exc}, kontynuuję pełen scrape")
        if not all_sessions:
            print("BŁĄD: Nie znaleziono sesji. Strona mogła zmienić format.")
            print(f"Sprawdź ręcznie: {SESSIONS_URL}")
            sys.exit(1)

        if args.max_sessions > 0:
            all_sessions = all_sessions[:args.max_sessions]
            print(f"  (ograniczono do {args.max_sessions} sesji)")

        if args.dry_run:
            print("\nZnalezione sesje:")
            for s in all_sessions:
                kid = assign_kadencja(s["date"]) or "?"
                print(f"  {s['number']:>8} | {s['date'] or '???'} | {kid} | {s['url']}")
            return

        # Tryb eksploracji
        if args.explore:
            print(f"\n[explore] Pobieram stronę sesji: {all_sessions[0]['url']}")
            soup = fetch(all_sessions[0]["url"], wait_for="article, .portlet-body", save_as="debug_session.html")
            print(f"\n--- Wszystkie linki na stronie sesji ---")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)[:100]
                if text and not href.startswith("javascript"):
                    print(f"  {text:<100} -> {href}")
            return

        # 2. Pobierz głosowania (lub załaduj z istniejącego data.json)
        out_path = Path(args.output)
        if args.only_transcripts:
            # Załaduj głosowania z istniejącego data.json
            if not out_path.exists():
                print(f"BŁĄD: --only-transcripts wymaga istniejącego {args.output}")
                sys.exit(1)
            print(f"\n[1/2] Ładowanie głosowań z {args.output}...")
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
            all_votes = []
            for kad in existing["kadencje"]:
                all_votes.extend(kad["votes"])
            print(f"  Załadowano {len(all_votes)} głosowań")
        else:
            docx_dir = out_path.parent / "docx_cache"
            docx_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n[2/4] Pobieranie i parsowanie wyników głosowań...")
            _mark("setup_docx_dir")
            all_votes = []
            for session in all_sessions:
                votes = process_session_docx(session, docx_dir)
                all_votes.extend(votes)
            _mark("fetch_and_parse_votes")

            print(f"  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

            if not all_votes:
                print("UWAGA: Nie znaleziono głosowań. Użyj --explore żeby zbadać strukturę strony sesji.")
                sys.exit(1)

        # 3. Pobierz transkrypcje stenogramów. Cache DOCX/PDF w scratch (obok
        # cache HTML), NIE w docs/ — inaczej deploy zsynchronizowałby je na S3.
        transcript_dir = (Path(args.cache_dir).parent / "transcript_cache"
                          if getattr(args, "cache_dir", None)
                          else Path(args.output).parent / "transcript_cache")
        transcript_dir.mkdir(parents=True, exist_ok=True)

        step = "[2/2]" if args.only_transcripts else "[3/4]"
        print(f"\n{step} Pobieranie transkrypcji stenogramów...")
        session_speakers: dict[str, list[dict]] = {}  # session_date -> speakers (agregat)
        session_turns: dict[str, list[dict]] = {}      # session_date -> tury (pełna treść)
        # Debug: sprawdź pierwszą sesję pod kątem transkrypcji
        if all_sessions:
            s0 = all_sessions[0]
            soup0 = _get_session_soup(s0)
            all_a = soup0.find_all("a", href=True)
            all_links = [(a["href"], a.get_text(strip=True)[:80]) for a in all_a]
            transcript_links = [(h, t) for h, t in all_links
                                if "transkrypcja" in h.lower() or "stenogram" in h.lower()
                                or "transkrypcja" in t.lower() or "stenogram" in t.lower()]
            # Liferay: .docx/.pdf w środku URL, nie na końcu
            doc_links = [(h, t) for h, t in all_links
                         if any(ext in h.lower() for ext in [".docx", ".pdf", ".xlsx"])]
            print(f"  Debug sesja {s0['number']}: {len(all_links)} linków, {len(doc_links)} docs, {len(transcript_links)} transkrypcji")
            if transcript_links:
                for href, text in transcript_links[:10]:
                    print(f"    transkrypcja: [{text[:60]}] -> {href[-100:]}")
            if doc_links:
                for href, text in doc_links[:10]:
                    print(f"    doc: [{text[:60]}] -> {href[-100:]}")
            # Szukaj sekcji z protokołami
            proto_links = [(h, t) for h, t in all_links
                           if any(kw in t.lower() for kw in ["protokół", "protokol", "dodatkowe", "transkryp", "steno"])]
            if proto_links:
                print(f"    Linki protokół/dodatkowe ({len(proto_links)}):")
                for href, text in proto_links[:10]:
                    print(f"      [{text[:60]}] -> {href[-100:]}")
            if not transcript_links and not doc_links:
                print(f"    UWAGA: Brak linków do dokumentów i transkrypcji!")
                print(f"    Pierwsze 10 linków na stronie:")
                for href, text in all_links[:10]:
                    print(f"      [{text[:60]}] -> {href[-80:]}")
        from lib_stenogram import aggregate_speakers
        for session in all_sessions:
            turns = process_session_transcript(session, transcript_dir)
            if turns:
                session_turns[session["date"]] = turns
                session_speakers[session["date"]] = [
                    {"name": s["name"], "statements": s["statements"], "words": s["words"]}
                    for s in aggregate_speakers(turns)
                ]
        print(f"  Transkrypcje: {len(session_speakers)}/{len(all_sessions)} sesji")

        # 4. Buduj output per kadencja
        print(f"\n[4/4] Budowanie pliku wyjściowego...")
        profiles = load_profiles(args.profiles)
        if profiles:
            print(f"  Załadowano profile: {len(profiles)} radnych")

        # Kluby z oficjalnej strony Rady PRZED budową statystyk, żeby
        # zgodność z klubem / rebelie / club_majority liczyły się na
        # aktualnym przypisaniu (np. Paweł Lech od 2026 niezrzeszony,
        # profiles.json miał zamrożone "KO" z seeda).
        bip_clubs = scrape_clubs()
        if bip_clubs:
            _changed = 0
            for _pname, _prof in profiles.items():
                _hit = bip_clubs.get(_name_key(_pname))
                if _hit and _prof.get("club") != _hit["club"]:
                    print(f"  Klub: {_pname}: {_prof.get('club')} -> {_hit['club']}")
                    _prof["club"] = _hit["club"]
                    _changed += 1
            if _changed:
                print(f"  Zaktualizowano kluby {_changed} radnych wg BIP")

        # Grupuj sesje i głosowania per kadencja (sortuj od najstarszej)
        kadencje_output = []
        target_kadencje_sorted = sorted(target_kadencje, key=lambda k: KADENCJE[k]["start"])
        for kid in target_kadencje_sorted:
            kinfo = KADENCJE[kid]
            k_sessions = [s for s in all_sessions if assign_kadencja(s["date"]) == kid]
            k_votes = [v for v in all_votes if assign_kadencja(v["session_date"]) == kid]
            if not k_votes:
                print(f"  Kadencja {kid}: brak głosowań — pomijam")
                continue
            kad_out = build_kadencja_output(kid, k_sessions, k_votes, profiles, session_speakers)
            if session_turns:
                _write_warszawa_transcripts(kad_out, session_turns, Path(args.output).parent, kid)
            kadencje_output.append(kad_out)

        default_kid = target_kadencje[0]

        # Stub'y dla historycznych kadencji znanych z KADENCJE, ale których
        # aktualnie nie scrape'ujemy (np. domyślny run --kadencja 2024-2029
        # nie tyka 2018-2024). Stub idzie tylko do data.json index, a save
        # _split_output nie nadpisuje archiwalnego kadencja-{id}.json bo
        # brak `sessions`. Dzięki temu SPA widzi historyczną kadencję w
        # RAW.kadencje i fetchuje ją po wejściu na stary URL głosowania.
        scraped_ids = {kad["id"] for kad in kadencje_output}
        for hist_kid, hist_meta in KADENCJE.items():
            if hist_kid in scraped_ids:
                continue
            kadencje_output.append({
                "id": hist_kid,
                "label": hist_meta.get("label", f"Kadencja {hist_kid}"),
            })

        output = {
            "generated": datetime.now().isoformat(),
            "default_kadencja": default_kid,
            "kadencje": kadencje_output,
        }

        out_path = Path(args.output)
        save_split_output(output, out_path)
        _mark("save_output")

        # Zapisz signature listy sesji żeby następny run mógł skipnąć fetche
        # jeśli nic się nie zmieniło.
        if _CACHE_DIR is not None and all_sessions:
            sig_path = _CACHE_DIR.parent / "session_signature.json"
            try:
                import hashlib as _h
                sig_input = "|".join(
                    sorted(f"{s.get('number','')}:{s.get('date','')}" for s in all_sessions)
                )
                sig = _h.sha256(sig_input.encode("utf-8")).hexdigest()
                sig_path.parent.mkdir(parents=True, exist_ok=True)
                sig_path.write_text(
                    json.dumps({
                        "sig_v2": sig,
                        "kadencje": target_kadencje,
                        "kadencje_known": sorted(KADENCJE.keys()),
                        "count": len(all_sessions),
                    }),
                    encoding="utf-8",
                )
            except Exception:
                pass

        print(f"\nGotowe! Zapisano do {out_path}")
        for kad in output["kadencje"]:
            total_v = len(kad["votes"])
            named_v = sum(1 for v in kad["votes"] if sum(len(nv) for nv in v["named_votes"].values()) > 0)
            print(f"  {kad['id']}: {len(kad['sessions'])} sesji, {total_v} głosowań ({named_v} z wynikami imiennymi), {len(kad['councilors'])} radnych")

        # Merge voting + activity stats into profiles.json
        merge_stats_to_profiles(args.profiles, output, bip_clubs)
        _mark("merge_profiles")

        # Timing breakdown
        print(f"\n=== Timing ===")
        prev = 0.0
        for name, total in _timings:
            delta = total - prev
            print(f"  {name:>28}: +{delta:7.1f}s  (cumulative {total:.1f}s)")
            prev = total

    finally:
        close_browser()


if __name__ == "__main__":
    main()
