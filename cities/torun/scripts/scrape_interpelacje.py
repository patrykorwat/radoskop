#!/usr/bin/env python3
"""Scraper interpelacji, wniosków i zapytań radnych Rady Miasta Torunia.

Źródło: BIP Torunia — rejestr "Interpelacje, wnioski, zapytania radnych".

    https://bip.torun.pl/interpelacje/29500

Po co: Rada Miasta Torunia nie publikuje interpelacji na eSesja (brak portalu),
tylko w rejestrze na BIP.

Struktura (BIP CMS Logonet):
  * Listing: /interpelacje/{page}/10, 10 rekordów na stronę (~123 strony).
    Każdy rekord = div > table.table-borderless:
        caption (visuallyhidden): "Interpelacja/Wniosek/Zapytanie w sprawie: <temat>"
        tr "Typ wystąpienia":  <a href="/interpelacja/{id}/{slug}">Interpelacja z dnia DD.MM.RRRR</a>
        tr "Tożsamość radnego": <imię nazwisko>
    Paginacja: /interpelacje/{page}/10 (page 1..N).
  * Szczegóły: /interpelacja/{id}/{slug}:
        "Typ wystąpienia", "Tożsamość radnego",
        Załączniki: <a href=".../attachments/download/{id}">opis pliku</a>
        - treść interpelacji/wniosku/zapytania (pierwszy załącznik bez "odpowiedź")
        - "odpowiedź ..." (załącznik z "odpowiedź" w nazwie) = odpowiedz_url

typ (Radoskop): interpelacja / zapytanie / wniosek  (jak bydgoszcz).
Klub radnego z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także VIII kadencja
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

BASE_URL = "https://bip.torun.pl"
LISTING_TPL = f"{BASE_URL}/interpelacje/{{page}}/10"
MAX_PAGES = 130
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> tuple[dict, dict]:
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


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
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
    time.sleep(DELAY)
    return fetch_text(session, LISTING_TPL.format(page=page))


# ---------------------------------------------------------------------------
# Listing parsing
# ---------------------------------------------------------------------------

# Każdy rekord: blok <div> z tabelą; znajdujemy przez anchor szczegółów.
# Grupa 1 = URL, grupa 2 = label linku (np. "Interpelacja z dnia 13.08.2026").
_DETAIL_RE = re.compile(
    r'href="(https://bip\.torun\.pl/interpelacja/\d+/[^"]+)"[^>]*>(.*?)</a>',
    re.S,
)


def _row_value(block: str, label: str) -> str:
    """Wartość komórki td dla wiersza o danym th (w obrębie bloku jednego rekordu)."""
    m = re.search(
        rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>',
        block, re.S | re.I,
    )
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", txt).strip()


def parse_listing(html: str) -> list[dict]:
    """Zwraca listę rekordów z listingu: {url, typ, radny, data_label}."""
    if not html:
        return []
    out = []
    # Podziel page'a na bloki po anchorach szczegółów.
    for m in _DETAIL_RE.finditer(html):
        url = m.group(1)
        start = m.start()
        # Blok: od tego anchor'a do następnego anchor'a szczegółów (lub końca).
        end_m = _DETAIL_RE.search(html, m.end())
        block = html[start:end_m.start() if end_m else start + 4000]
        # typ + data z labelu linku
        label = re.sub(r"\s+", " ", m.group(2)).strip()
        low = label.lower()
        if "zapytanie" in low:
            typ = "zapytanie"
        elif "wniosek" in low:
            typ = "wniosek"
        else:
            typ = "interpelacja"
        d = re.search(r"z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", label, re.I)
        date_label = f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else ""
        radny = _row_value(block, "Tożsamość radnego")
        out.append({
            "url": url,
            "typ": typ,
            "radny": radny,
            "date_label": date_label,
        })
    return out


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

_FILE_RE = re.compile(r'<a[^>]*href="(https://bip\.torun\.pl/attachments/download/\d+)"[^>]*>(.*?)</a>', re.S)


def _attr_value(html: str, label: str) -> str:
    m = re.search(
        rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>',
        html, re.S | re.I,
    )
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", txt).strip()


def parse_detail(html: str, listing: dict) -> dict:
    """Buduje rekord Radoskop z treści strony szczegółów + danych z listingu."""
    typ = listing["typ"]
    radny = listing["radny"]
    if not radny:
        radny = _attr_value(html, "Tożsamość radnego")
    if not typ and _attr_value(html, "Typ wystąpienia"):
        typ_raw = _attr_value(html, "Typ wystąpienia").lower()
        if "zapytanie" in typ_raw:
            typ = "zapytanie"
        elif "wniosek" in typ_raw:
            typ = "wniosek"
        else:
            typ = "interpelacja"
    typ = typ or "interpelacja"

    files = [
        (re.sub(r"<[^>]+>", " ", label).strip(), href.strip())
        for href, label in _FILE_RE.findall(html)
    ]

    # treść = pierwszy załącznik bez "odpowiedź"; odpowiedź = pierwszy z "odpowiedź"
    tresc_url = ""
    odpowiedz_url = ""
    przedmiot = listing_theme = ""
    for label, href in files:
        low = label.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        else:
            if not tresc_url:
                tresc_url = href
                przedmiot = label
    # jeżeli lista z dedykowanym tematem w bip_url nie ma, weź pierwszego załącznika
    if not przedmiot and files:
        przedmiot = files[0][0]

    data_wplywu = listing["date_label"] or ""
    # Data z "Data wytworzenia" w metryczce jako fallback
    if not data_wplywu:
        m = re.search(r"Data wytworzenia:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", html)
        if m:
            data_wplywu = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # DD-MM-RRRR -> RRRR-MM-DD
    m2 = re.fullmatch(r"(\d{1,2})[-.](\d{1,2})[-.](\d{4})", data_wplywu or "")
    if m2:
        data_wplywu = f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"

    rok = 0
    if data_wplywu:
        try:
            rok = int(data_wplywu[:4])
        except ValueError:
            rok = 0

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    # cri = id z URL
    m_id = re.search(r"/interpelacja/(\d+)/", listing["url"])
    cri = m_id.group(1) if m_id else ""

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": listing["url"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji, wniosków i zapytań radnych z BIP Torunia"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="maks. liczba stron listingu do przejścia")
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też VIII kadencję (rok < 2024)")
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Torunia ===")
    seen: dict[str, dict] = {}
    empty_streak = 0
    page = 1
    while page <= args.max_pages:
        html = fetch_listing(session, page)
        items = parse_listing(html)
        new = [it for it in items if it["url"] not in seen]
        if not new:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        for it in new:
            seen[it["url"]] = it
        if page % 10 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)} znalezionych)")
        page += 1
    print(f"  Listing: {len(seen)} rekordów (do strony {page - 1})")

    records = []
    for url, listing in seen.items():
        # Optymalizacja: gdy filtrujemy do bieżącej kadencji, pomiń szczegóły
        # rekordów starszych niż min_rok (oszczędza ~1000 fetchy).
        if min_rok and listing.get("date_label"):
            ly = listing["date_label"][:4]
            if ly.isdigit() and int(ly) < min_rok:
                continue
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, listing)
        if not rec:
            continue
        if rec["rok"] == 0:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    wn = sum(1 for r in records if r["typ"] == "wniosek")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Wnioski:      {wn}")
    print(f"Odpowiedzi:   {answered}")
    print(f"Razem:        {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
