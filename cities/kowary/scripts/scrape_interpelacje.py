#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Kowarach.

Źródło: BIP Kowar (CMS Logonet) — kategoria "Interpelacje / Zapytania".

    https://bip.kowary.pl/artykuly/interpelacje-zapytania

Po co: Rada Miejska w Kowarach nie publikuje interpelacji na eSesja (moduł
interpelacji nieaktywny), tylko w formie uporządkowanego rejestru na BIP
Logonet. Kategoria ma wbudowany filtr typu (Wniosek/Zapytanie/Interpelacja/Petycja).

Struktura:
  * Listing = kategoria z tabelami, każdy rekord ma link do strony szczegółów:
        /interpelacja/i-{...slug...}
  * Szczegóły = tabela <th>...</th><td>...</td> z polami:
        Typ wystąpienia, W sprawie, Kadencja, Tożsamość radnego,
        Data wytworzenia  (DD.MM.RRRR)
    oraz załączniki PDF (<a href="/attachments/.../download/...">):
        "interpelacja nr N"  (treść)
        "odpowiedź - interpelacja nr N"  (odpowiedź)
    (rzadko: "zapytanie ..." / "odpowiedź - zapytanie ...").

Output: lista rekordów w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego bierzemy z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/interp/kowary
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

BASE = "https://bip.kowary.pl"
# Kategoria rejestru (wg menu Rada Miejska -> Interpelacje / Zapytania).
REGISTER_URL = f"{BASE}/artykuly/interpelacje-zapytania"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MAX_PAGES = 20
MIN_ROK_DEFAULT = 2024

_DEBUG = False
_CLUB_ASSIGN = {}
_CLUBS = {}


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> None:
    global _CLUB_ASSIGN, _CLUBS
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return
    _CLUB_ASSIGN = cfg.get("club_assignments", {}) or {}
    _CLUBS = cfg.get("clubs", {}) or {}


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


def _abs(url: str) -> str:
    return url if url.startswith("http") else BASE + url


def fetch_text(session: requests.Session, url: str) -> str:
    """Fetch z politeness delay + retry (Logonet bywa płochliwy na 5xx)."""
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Link do szczegółów interpelacji na liście kategorii.
_DETAIL_RE = re.compile(r'href="(/interpelacja/[^"]+)"')


def parse_listing(html: str) -> list[str]:
    if not html:
        return []
    out = []
    for m in _DETAIL_RE.finditer(html):
        u = m.group(1)
        if u not in out:
            out.append(u)
    return [_abs(u) for u in out]


# Tabela pól na stronie szczegółów: <th>nazwa</th> <td>wartość</td>
_TH_TD_RE = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)
# Załączniki PDF
_ATTACH_RE = re.compile(r'<a\s+[^>]*href="(/attachments/[^"]+)"[^>]*>(.*?)</a>', re.S)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def normalize_date(dd_mm_yyyy: str) -> str:
    """DD.MM.RRRR (Logonet używa kropek) -> RRRR-MM-DD."""
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_detail(html: str, bip_url: str) -> dict | None:
    if not html:
        return None
    fields = {}
    for name, value in _TH_TD_RE.findall(html):
        fields[_clean(name).rstrip(":")] = _clean(value)

    typ_raw = fields.get("Typ wystąpienia", "").lower()
    przedmiot = fields.get("W sprawie", "")
    kadencja = fields.get("Kadencja", "") or "2024-2029"
    radny = fields.get("Tożsamość radnego", "")
    data_wplywu = normalize_date(fields.get("Data wytworzenia", ""))

    # Typ: Interpelacja / Zapytanie / Wniosek / Petycja
    if "zapytanie" in typ_raw:
        typ = "zapytanie"
    elif "wniosek" in typ_raw or "petycja" in typ_raw:
        typ = "wniosek"
    else:
        typ = "interpelacja"

    rok = 0
    try:
        rok = int(data_wplywu[:4]) if data_wplywu else 0
    except ValueError:
        rok = 0

    # Załączniki: treść (interpelacja/zapytanie, bez "odpowiedź") i odpowiedź.
    tresc_url = ""
    odpowiedz_url = ""
    for href, label_html in _ATTACH_RE.findall(html):
        label = _clean(label_html).lower()
        if "odpowied" in label:
            if not odpowiedz_url:
                odpowiedz_url = _abs(href.strip())
        elif ("interpelacj" in label or "zapytan" in label or "wniosk" in label) and not tresc_url:
            tresc_url = _abs(href.strip())
        elif not tresc_url:
            tresc_url = _abs(href.strip())

    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    # cri: użyjmy numeru z nazwy pliku treści ("interpelacja nr N") lub slugów.
    cri = ""
    m = re.search(r"nr\s*([0-9]+)", tresc_url or "")
    if m:
        cri = m.group(1)
    if not cri:
        m2 = re.search(r"/interpelacja/([^/]+)$", bip_url)
        cri = (m2.group(1) if m2 else "").replace("-", "")

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": bip_url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Kowar"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też wcześniejsze kadencje; domyślnie tylko 2024-2029",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    _load_clubs()
    session = _session()

    print("=== Interpelacje / Zapytania — BIP Kowar ===")
    seen: dict[str, str] = {}
    page = 1
    empty_streak = 0
    while page <= MAX_PAGES:
        url = REGISTER_URL if page == 1 else REGISTER_URL
        time.sleep(DELAY)
        html = fetch_text(session, url)
        links = parse_listing(html)
        new = [u for u in links if u not in seen]
        _log(f"  listing {page}: {len(links)} linków, nowych {len(new)}")
        if not new:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
            for u in new:
                seen[u] = ""
        # Rejestr nie ma paginacji (jedna kategoria) — po pierwszej stronie kończymy.
        break
    print(f"  Listing: {len(seen)} rekordów w rejestrze")

    records = []
    for url in seen:
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            continue
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

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
