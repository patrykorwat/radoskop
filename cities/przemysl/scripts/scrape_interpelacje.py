#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Przemyślu.

Źródło: BIP Przemysłu — "Rejestr interpelacji i zapytań Radnych Rady Miejskiej
w Przemyślu (rok 2022 - obecnie), kierowanych do Prezydenta Miasta Przemyśla
oraz udzielonych odpowiedzi".

    https://bip.przemysl.pl/80385/rejestr-interpelacji-i-zapytan-...

Po co: Rada Miejska w Przemyślu NIE publikuje interpelacji na eSesja
(moduł "Interpelacje i zapytania" jest nieaktywny — strona zwraca "Brak
aktywności lub moduł nieaktywny"), tylko w formie rejestru na BIP.

Struktura:
  * Listing = strona CCT z paginacją `?Page=N&cct-search=&is_content_type_search=1`.
    Każdy rekord to link do strony szczegółów:
        /{id}/interpelacja-w-sprawie-...html
  * Szczegóły = strona CCT z atrybutami (div.cct-page__attribute):
        Nr Interpelacji::, Imię:, Nazwisko:, Rok:, Temat Interpelacji:,
        Data złożenia: (DD-MM-RRRR), Data Załatwienia Sprawy: ("-" = bez odp.)
    oraz załączniki PDF (a.fileLink): treść, "Odpowiedź na Interpelację...",
    "Nawiązanie do Interpelacji...".

Output: lista rekordów w formacie Radoskop (ten sam schemat co Warszawa/
Bydgoszcz):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego jest brany z config.json (club_assignments -> clubs), tak samo
jak w scrape_przemysl.py.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także VIII kadencja (2022-2023)
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

# Rejestr może być przeniesiony na BIP — aktualizuj ten URL jeżeli 404.
# Działa też bez niego: podajemy go tylko jako bazę paginacji.
REJESTR_URL = (
    "https://bip.przemysl.pl/80385/rejestr-interpelacji-i-zapytan-radnych-rady-"
    "miejskiej-w-przemyslu-rok-2022-obecnie-kierowanych-do-prezydenta-miasta-"
    "przemysla-oraz-udzielonych-odpowiedzi.html"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MAX_PAGES = 120
# Domyślnie tylko bieżąca kadencja (IX, 2024-2029) — Radoskop Przemyśl śledzi
# wyłącznie ją. VIII kadencja (2022-2023) dostępna jest przez --all.
MIN_ROK_DEFAULT = 2024

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> tuple[dict, dict]:
    """(club_assignments, clubs) z config.json miasta."""
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    """Fetch z politeness delay + retry na przejściowych 403/5xx."""
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                # BIP Przemyśl jest UTF-8 (diakrytyki się poprawnie parsują).
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def fetch_listing(session: requests.Session, page: int) -> str:
    url = f"{REJESTR_URL}?Page={page}&cct-search=&is_content_type_search=1"
    time.sleep(DELAY)
    return fetch_text(session, url)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DETAIL_RE = re.compile(r'href="(https://bip\.przemysl\.pl/\d+/interpelacja[^"]+)"')


def parse_listing(html: str) -> list[str]:
    if not html:
        return []
    out = []
    for m in _DETAIL_RE.finditer(html):
        u = m.group(1)
        if u not in out:
            out.append(u)
    return out


_ATTR_RE = re.compile(
    r'<div class="cct-page__name">\s*(.*?)\s*</div>\s*'
    r'<div class="cct-page__value">\s*(.*?)\s*</div>',
    re.S,
)
_FILE_RE = re.compile(r'<a class="fileLink"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def parse_detail(html: str, bip_url: str) -> dict | None:
    if not html:
        return None
    attrs = {}
    for name, value in _ATTR_RE.findall(html):
        attrs[re.sub(r"\s+", " ", name).strip()] = re.sub(r"\s+", " ", value).strip()

    nr = attrs.get("Nr Interpelacji::", attrs.get("Nr Zapytania::", "")).strip()
    imie = attrs.get("Imię:", "")
    nazwisko = attrs.get("Nazwisko:", "")
    rok = attrs.get("Rok:", "")
    temat = attrs.get("Temat Interpelacji:", attrs.get("Temat Zapytania:", ""))
    data_zlozenia = attrs.get("Data złożenia:", "")
    data_zalatwienia = attrs.get("Data Załatwienia Sprawy:", "")

    # Nagłówek strony (h1) — najpewniejsze źródło typu interpelacja/zapytanie.
    header_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    header = re.sub(r"<[^>]+>", " ", header_m.group(1)).strip() if header_m else ""

    files = [
        (re.sub(r"<[^>]+>", " ", label).strip(), href.strip())
        for href, label in _FILE_RE.findall(html)
    ]

    try:
        rok_int = int(rok)
    except (TypeError, ValueError):
        rok_int = 0

    # typ: "zapytanie" vs "interpelacja" — z tematu / nagłówka.
    hay = " ".join([header, temat]).lower()
    typ = "zapytanie" if "zapytanie" in hay else "interpelacja"

    kadencja = "2024-2029" if rok_int >= 2024 else "2018-2024"

    # radny: pierwszy autor (imię + nazwisko); przy współautorach bierzemy
    # pierwszy segment przed przecinkiem (dane BIP potrafią wymieszać
    # imiona/nazwiska wielu radnych w tych polach — np. nr 71).
    full_name = " ".join([imie, nazwisko]).strip()
    parts = [p.strip() for p in re.split(r"[,\n]+", full_name) if p.strip()]
    radny = parts[0] if parts else full_name

    data_wplywu = normalize_date(data_zlozenia)
    data_odpowiedzi = "" if data_zalatwienia in ("", "-", "—") else normalize_date(data_zalatwienia)

    # Załączniki: treść = pierwszy z "interpelacja"/"zapytanie" (bez
    # "nawiązanie"/"odpowiedź"); odpowiedź = pierwszy z "odpowiedź".
    tresc_url = ""
    odpowiedz_url = ""
    for label, href in files:
        low = label.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif "nawiązanie" in low or "nawiazanie" in low:
            continue
        elif ("interpelacj" in low or "zapytan" in low) and not tresc_url:
            tresc_url = href
        elif not tresc_url and not odpowiedz_url:
            # fallback: pierwszy załącznik jako treść
            tresc_url = href

    return {
        "cri": nr,
        "typ": typ,
        "rok": rok_int,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": temat,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if data_odpowiedzi else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": bip_url,
    }


def normalize_date(dd_mm_yyyy: str) -> str:
    """DD-MM-RRRR -> RRRR-MM-DD (sortowanie chronologiczne w frontendzie)."""
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG, MIN_ROK
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Przemysłu"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też VIII kadencję (rok 2022-2023); domyślnie tylko 2024-2029",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)

    session = _session()

    print("=== Interpelacje — BIP Przemyślu ===")
    seen: dict[str, str] = {}  # bip_url -> detail html (dedupe)
    empty_streak = 0
    page = 1
    while page <= MAX_PAGES:
        html = fetch_listing(session, page)
        links = parse_listing(html)
        new_links = [u for u in links if u not in seen]
        _log(f"  strona {page}: {len(links)} linków, nowych: {len(new_links)}")
        if not new_links:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        for u in new_links:
            seen[u] = ""
        if page % 10 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)} znalezionych)")
        page += 1
    print(f"  Listing: {len(seen)} interpelacji w rejestrze (do strony {page - 1})")

    records = []
    fetched = 0
    for i, url in enumerate(seen, start=1):
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            continue
        fetched += 1
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if fetched % 50 == 0:
            print(f"  szczegóły: {fetched}...")

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    # Statystyki
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
