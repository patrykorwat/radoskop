#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Łebie.

Źródło: platforma rady "System Rada" Gminy Miejskiej Łeba.

    https://rada.leba.eu/interpelacje

eSesja (https://leba.esesja.pl/interpelacje_i_zapytania) — moduł NIEAKTYWNY
("Brak aktywności lub moduł nieaktywny"). BIP z config.json (https://bip.leba.pl/)
NIE rozwiązuje się w DNS (martwa domena); rzeczywiste BIP gminy to
https://bipleba.nv.pl (nie zawiera rejestru interpelacji). Jedynym publicznym,
aktualnym źródłem interpelacji jest platforma rady.

Struktura listingu: https://rada.leba.eu/interpelacje — linki do detali:
    /interpelacje/interpelacja/{id}
Detal — blok <ul class="list-group"> z polami:
    Rodzaj           -> Interpelacja | Zapytanie | Wniosek
    Skierowane do
    Interpelujący    -> radny
    Data wpływu      -> DD-MM-YYYY (data wystąpienia)
    Data przekazania
    Treść            -> przedmiot/treść
    Załączniki       -> PDF treści (pierwszy załącznik = tresc_url)
oraz sekcja "Odpowiedź" (jeśli udzielona):
    Odpowiadający / Wydział merytoryczny / Data odpowiedzi
    Załączniki       -> PDF odpowiedzi (pierwszy = odpowiedz_url)
Tytuł strony (h1) = przedmiot.

Klub radnego z config.json (club_assignments -> clubs).

Output: rekordy w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/interp/leba
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://rada.leba.eu"
REGISTER_URL = f"{BASE}/interpelacje"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029

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
    code = _CLUB_ASSIGN.get((radny or "").strip(), "")
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
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DETAIL_RE = re.compile(r'href="(/interpelacje/interpelacja/\d+)"')


def parse_listing(html: str) -> list[str]:
    if not html:
        return []
    out = []
    for m in _DETAIL_RE.finditer(html):
        u = m.group(1)
        if u not in out:
            out.append(u)
    return [_abs(u) for u in out]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def normalize_date(dd_mm_yyyy: str) -> str:
    """DD-MM-YYYY (platforma rady.leba.eu używa myślników) -> RRRR-MM-DD."""
    m = re.search(r"(\d{1,2})[-. /](\d{1,2})[-. /](\d{4})", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _typ_for(rodzaj_raw: str) -> str:
    r = (rodzaj_raw or "").lower()
    if "zapytani" in r:
        return "zapytanie"
    if "wniosk" in r:
        return "wniosek"
    if "interpelacj" in r:
        return "interpelacja"
    return "interpelacja"


def _split_content(html: str) -> tuple[BeautifulSoup, BeautifulSoup, BeautifulSoup] | None:
    """Zwraca (kolumna-treści, h2-odpowiedzi | None, pełny soup ze stroną)."""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    main = soup.find("main") or soup.body
    col = None
    for c in main.find_all("div"):
        if "col-lg-8" in " ".join(c.get("class", [])):
            col = c
            break
    if col is None:
        return None
    # przedmiot = h1 całej strony
    # sekcja odpowiedzi zaczyna się od <h2>Odpowiedź</h2>
    odp_h2 = col.find("h2")
    while odp_h2 and "Odpowied" not in odp_h2.get_text(" ", strip=True):
        odp_h2 = odp_h2.find_next("h2")
    return col, odp_h2, soup


def _list_group_fields(root: BeautifulSoup) -> dict:
    """Z listy <ul> nagłówków <strong>label:</strong> value -> dict label->value.
    Dla 'Załączniki' zwraca listę (href, label)."""
    fields = {}
    for li in root.find_all("li", class_="list-group-item"):
        strong = li.find("strong")
        if strong is None:
            continue
        label = _clean(strong.get_text()).rstrip(":")
        strong.decompose()
        if "Załączniki" in label:
            links = []
            for a in li.find_all("a", href=True):
                links.append((a["href"].strip(), _clean(a.get_text())))
            fields[label] = links
        else:
            fields[label] = _clean(li.get_text(" ", strip=True))
    return fields


def _attachments(fields: dict) -> list:
    """Lista (href, label) z pola o nazwie 'Załączniki...'."""
    for k, v in fields.items():
        if "Załączniki" in k and isinstance(v, list):
            return v
    return []


def parse_detail(html: str, url: str) -> dict | None:
    if not html:
        return None
    parsed = _split_content(html)
    if parsed is None:
        return None
    col, odp_h2, page_soup = parsed
    h1 = page_soup.find("h1")
    przedmiot = _clean(h1.get_text(" ", strip=True)) if h1 else ""

    # Kolumna treści (col-lg-8) zawiera wyłącznie listy grupowane:
    #   [0] = wystąpienie (Rodzaj/Skierowane/Interpelujący/Daty/Treść/Załączniki)
    #   [1] = odpowiedź (jeśli udzielona; gdy jest sekcja <h2>Odpowiedź</h2>)
    uls = col.find_all("ul", class_="list-group")
    sub_fields = _list_group_fields(uls[0]) if uls else {}
    resp_fields = {}
    if odp_h2 is not None and len(uls) >= 2:
        resp_fields = _list_group_fields(uls[1])

    typ = _typ_for(sub_fields.get("Rodzaj", ""))
    radny = sub_fields.get("Interpelujący", "") or sub_fields.get("Interpelujacy", "")
    data_wplywu = normalize_date(sub_fields.get("Data wpływu", ""))
    data_odpowiedzi = normalize_date(resp_fields.get("Data odpowiedzi", ""))

    rok = 0
    try:
        rok = int(data_wplywu[:4]) if data_wplywu else 0
    except ValueError:
        rok = 0

    trec = _attachments(sub_fields)
    tresc_url = _abs(trec[0][0]) if trec else ""
    resp_att = _attachments(resp_fields)
    odpowiedz_url = _abs(resp_att[0][0]) if resp_att else ""

    odpowiedz_status = "Udzielono" if (odpowiedz_url or "Data odpowiedzi" in resp_fields) else "Nie udzielono"

    m = re.search(r"/interpelacje/interpelacja/(\d+)", url)
    cri = m.group(1) if m else ""

    kadencja = "2024-2029" if rok >= 2024 else ""
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
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych — Rada Miasta Łeby (rada.leba.eu)"
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

    print("=== Interpelacje / Zapytania — Rada Miasta Łeby (rada.leba.eu) ===")

    time.sleep(DELAY)
    listing_html = fetch_text(session, REGISTER_URL)
    links = parse_listing(listing_html)
    print(f"  Listing: {len(links)} rekordów")

    records = []
    for url in links:
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            print(f"  [skip] nie sparsowano: {url}")
            continue
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        records.append(rec)
        _log(f"  {rec['cri']} {rec['typ']} {rec['data_wplywu']} {rec['radny']}")

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
