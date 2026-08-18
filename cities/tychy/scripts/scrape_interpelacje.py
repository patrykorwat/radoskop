#!/usr/bin/env python3
"""Scraper interpelacji, wniosków i zapytań radnych Rady Miasta Tychy.

Źródło: BIP Tychy (WordPress/modyfikacja własna UM) — strona
    "Zapytania i interpelacje Radnych".

    https://bip.umtychy.pl/zapytania-i-interpelacje-radnych

Po co: portal eSesja (https://tychy.esesja.pl/interpelacje_i_zapytania) jest
NIEAKTYWNY ("Brak aktywności lub moduł nieaktywny"), więc źródłem jest BIP.

Struktura:
  * Listing (jedna strona, bieżąca kadencja 2024-2029):
      <a href="/zapytania-i-interpelacje-radnych/{id}">Tytuł</a>
    Tytuł: "Interpelacja radnego Ł. D. złożona 31 lipca 2026 r. w sprawie ..."
    (też: "Zapytanie ... złożone ...", "Wniosek ... złożony ...").
  * Szczegóły: /zapytania-i-interpelacje-radnych/{id} -> table.bip-record-details:
        Kadencja, "Opis krótki" (pełny przedmiot), Załączniki
        (ul > li: nazwa pliku + linki [Pobierz|Pokaż] index.php?action=PobierzPlik&id=N),
        metryczka "Data wytworzenia" (= data złożenia, RRRR-MM-DD).
    tresc = pierwszy załącznik bez "Odpowiedź"; odpowiedz = pierwszy z "Odpowiedź".

UWAGA: serwer BIP Tychy ma problem z certyfikatem SSL — fetch używa
verify=False (odpowiednik zachowania przeglądarki; ~300 OK).

typ (Radoskop): interpelacja / zapytanie / wniosek  (jak bydgoszcz).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache
"""

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE_URL = "https://bip.umtychy.pl"
LIST_URL = f"{BASE_URL}/zapytania-i-interpelacje-radnych"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MIN_ROK_DEFAULT = 2024

# Miesiące polskie (dla tytułów listingu jako fallback daty).
MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

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
    if not radny:
        return ""
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        # Fallback: dopasowanie po nazwisku (tytuły BIP podają imię w
        # dopełniaczu, np. "Lidii Gajdas" vs klucz "Lidia Gajdas").
        surname = radny.split()[-1] if radny.split() else ""
        for name, c in _CLUB_ASSIGN.items():
            if name.split() and name.split()[-1] == surname:
                code = c
                break
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=45, verify=False)
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


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

_DETAIL_LINK_RE = re.compile(
    r'href="(?:/[^"]*/)?zapytania-i-interpelacje-radnych/(\d+)"[^>]*>(.*?)</a>', re.S
)


def _title_info(title: str) -> dict:
    """Z tytułu listingu: {typ, radny, przedmiot, date_label}."""
    title = re.sub(r"\s+", " ", title).strip()
    low = title.lower()
    if low.startswith("zapytanie"):
        typ = "zapytanie"
    elif low.startswith("wniosek"):
        typ = "wniosek"
    else:
        typ = "interpelacja"

    # przedmiot po "w sprawie"
    przedmiot = ""
    m = re.search(r"w sprawie\s+(.*)$", title, re.I)
    if m:
        przedmiot = re.sub(r"[\s\u00a0]+$", "", m.group(1)).strip()

    # data z "złożona 31 lipca 2026 r." / "złożony DD miesiąc RRRR" / "z DD miesiąc RRRR"
    date_label = ""
    dm = re.search(
        r"\bzłożon[ae]?\s+(\d{1,2})\s+(\w+)\s+(\d{4})|\bz\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
        title, re.I,
    )
    if dm:
        if dm.group(1):
            day, mon, year = dm.group(1), dm.group(2).lower(), dm.group(3)
        else:
            day, mon, year = dm.group(4), dm.group(5).lower(), dm.group(6)
        month = MONTHS_PL.get(mon)
        if month:
            date_label = f"{year}-{month:02d}-{int(day):02d}"

    # radny: pierwszy aktor z tytułu (imię i nazwisko w formie z BIP).
    radny = ""
    for pat in [
        r"(?:radnego|radnej)\s+([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-ząćęłńóśźż\-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\-]+)?)",
        r"(?:Przewodniczące[g]?[ej]?\s+Rady\s+Miasta\s+|Wiceprzewodniczące[g]?[ej]?\s+Rady\s+Miasta\s+)([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-ząćęłńóśźż\-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\-]+)?)",
        r"radnych:\s*([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-ząćęłńóśźż\-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\-]+)?)",
    ]:
        m = re.search(pat, title)
        if m and m.group(1):
            radny = m.group(1).strip()
            break
    return {"typ": typ, "radny": radny, "przedmiot": przedmiot, "date_label": date_label}


def parse_listing(html: str) -> list[dict]:
    if not html:
        return []
    out = []
    seen = set()
    for m in _DETAIL_LINK_RE.finditer(html):
        detail_id = m.group(1)
        url = f"{BASE_URL}/zapytania-i-interpelacje-radnych/{detail_id}"
        # Tytuł może być owinięty w <p>...</p> — zdejmij tagi HTML.
        title = re.sub(r"<[^>]+>", " ", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        info = _title_info(title)
        info.update({"url": url, "detail_id": detail_id})
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

_ATT_LI_RE = re.compile(
    r"<li>\s*<div>.*?</div>\s*<div>\s*<span>(.*?)</span>"
    r"\s*<span>\s*\[.*?href=\"([^\"]*PobierzPlik[^\"]*)\"[^>]*>\s*Pokaż",
    re.S,
)


def _datum_wytworzenia(html: str) -> str:
    m = re.search(r"Data wytworzenia[^<]*</th>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})", html)
    if m:
        return m.group(1)
    m = re.search(r"Data wytworzenia\s*</th>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})", html)
    return m.group(1) if m else ""


def parse_detail(html: str, listing: dict) -> dict:
    if not html:
        return None

    # Opis krótki (pełny przedmiot)
    przedmiot = listing.get("przedmiot", "")
    om = re.search(r"<th>\s*Opis krótki\s*</th>\s*<td>(.*?)</td>", html, re.S)
    if om:
        p = re.sub(r"<[^>]+>", " ", om.group(1))
        p = unescape(p)
        p = re.sub(r"\s+", " ", p).strip()
        if p:
            # lepiej doprecyzować radnego/typ z pełnego opisu
            info = _title_info(p)
            przedmiot = info.get("przedmiot") or przedmiot
            listing = dict(listing)
            listing["radny"] = listing["radny"] or info.get("radny", "")
            listing["typ"] = listing["typ"] or info.get("typ", "interpelacja")

    # Załączniki: (nazwa, Pokaż-url)
    files = []
    for name, href in _ATT_LI_RE.findall(html):
        files.append((re.sub(r"\s+", " ", name).strip(),
                      urljoin(BASE_URL, unescape(href))))

    tresc_url = ""
    odpowiedz_url = ""
    for name, href in files:
        low = name.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif not tresc_url:
            tresc_url = href

    data_wplywu = _datum_wytworzenia(html) or listing.get("date_label", "")
    rok = 0
    if data_wplywu:
        try:
            rok = int(data_wplywu[:4])
        except ValueError:
            rok = 0

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    return {
        "cri": listing.get("detail_id", ""),
        "typ": listing["typ"],
        "rok": rok,
        "kadencja": kadencja,
        "radny": listing.get("radny", ""),
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(listing.get("radny", "")),
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
        description="Scraper interpelacji, wniosków i zapytań radnych z BIP Tychy"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też starsze rekordy (rok < 2024)")
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Tychy ===")
    html = fetch_text(session, LIST_URL)
    items = parse_listing(html)
    print(f"  Listing: {len(items)} rekordów (pojedyncza strona)")

    records = []
    for it in items:
        if min_rok and it.get("date_label"):
            if it["date_label"][:4].isdigit() and int(it["date_label"][:4]) < min_rok:
                continue
        time.sleep(DELAY)
        dhtml = fetch_text(session, it["url"])
        if not dhtml:
            print(f"  [skip] brak treści: {it['url']}")
            continue
        rec = parse_detail(dhtml, it)
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
