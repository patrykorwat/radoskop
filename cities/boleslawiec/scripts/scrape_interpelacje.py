#!/usr/bin/env python3
"""Scraper interpelacji i wniosków radnych Rady Miejskiej w Bolesławcu.

Źródło: BIP Bolesławca — "Rejestr interpelacji i wniosków" (CMS bip-gov.pl).

    http://www.um.boleslawiec.bip-gov.pl/public/?id=116897   (IX kadencja)

eSesja (https://boleslawiec.esesja.pl) ma moduł interpelacji NIEAKTYWNY
("Brak aktywności lub moduł nieaktywny"). Źródłem jest rejestr na BIP.

Struktura drzewa (strony id=...):
  * Kadencja (116897) -> lata (np. 116898=2024, 117888=2025, 118920=2026)
  * Rok -> miesiące (element_podkategorii -> id)
  * Miesiąc -> tabela rejestru z kolumnami:
        Nr | Imię i nazwisko | Dotyczy (link get_file.php => treść PDF)
          | Odpowiedź na interpelację (link "Odpowiedź PDF")
  Każdy wiersz tabeli = jedna interpelacja/wniosek.

Ograniczenie źródła: rejestr podaje wyłącznie miesiąc i rok złożenia
(nagłówek "Rejestr interpelacji i wniosków - maj 2024 r."), bez dnia — dlatego
data_wplywu pozostaje pusta, a wypełniane są rok i kadencja. Nie zmyślamy dnia.

Output: rekordy w formacie Radoskop (schemat jak Przemyśl/Bartoszyce):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
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

BASE = "http://www.um.boleslawiec.bip-gov.pl/public"
KADENCJA_ID = "116897"  # "Rejestr interpelacji i wniosków - IX kadencja" (2024-2029)
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


def _collapse(html: str) -> str:
    return re.sub(r">\s+<", "><", html)


_SUBPAGE_RE = re.compile(
    r'<li class="element_podkategorii">.*?href="/public/\?id=(\d+)"[^>]*>\s*([^<]+?)\s*</a>',
    re.S,
)


def _subpages(html: str) -> list[tuple[str, str]]:
    """[(id, nazwa)] podstron (lata lub miesiące)."""
    out = []
    for pid, name in _SUBPAGE_RE.findall(_collapse(html)):
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            out.append((pid, name))
    return out


# ---------------------------------------------------------------------------
# Parsing miesiąca (tabela rejestru)
# ---------------------------------------------------------------------------

_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def parse_month(html: str, rok: int, month_page_url: str) -> list[dict]:
    """Parsuje tabelę rejestru na stronie miesiąca -> rekordy Radoskop."""
    out = []
    html = _collapse(html)
    tbl = re.search(r"<table[^>]*>(.*?)</table>", html, re.S)
    if not tbl:
        return out
    body = tbl.group(1)
    for tr in _TR_RE.findall(body):
        if "<th" in tr:  # pomiń wiersz nagłówka tabeli
            continue
        raw_cells = _TD_RE.findall(tr)
        cells = [_clean(c) for c in raw_cells]
        if len(cells) < 3:
            continue
        nr_txt = cells[0]
        radny_raw = cells[1]
        if not radny_raw:
            continue
        if radny_raw.strip().lower() in ("imię i nazwisko", "imie i nazwisko"):
            continue  # wiersz nagłówka (niektóre miesiące mają <td> zamiast <th>)
        # Dotyczy: link treści + temat (surowy HTML komórki 2)
        dotyczy_html = raw_cells[2] if len(raw_cells) > 2 else ""
        a = re.search(r'href="(/public/get_file\.php\?id=\d+)"[^>]*>(.*?)</a>', dotyczy_html, re.S)
        if a:
            tresc_url = BASE + "/" + a.group(1).lstrip("/")
            temat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", a.group(2))).strip()
        else:
            tresc_url = ""
            temat = _clean(dotyczy_html)
        # Odpowiedź: ostatnia komórka (surowy HTML)
        odp_cell = raw_cells[3] if len(raw_cells) > 3 else ""
        a2 = re.search(r'href="(/public/get_file\.php\?id=\d+)"', odp_cell)
        odpowiedz_url = (BASE + "/" + a2.group(1).lstrip("/")) if a2 else ""

        radny = _clean_radny(radny_raw)

        cri = nr_txt or ""
        rok_int = rok
        records = {
            "cri": cri,
            "typ": "interpelacja",
            "rok": rok_int,
            "kadencja": "2024-2029" if rok_int >= 2024 else "2018-2023",
            "radny": radny,
            "przedmiot": temat,
            "data_wplywu": "",
            "klub": _club_for_radny(radny),
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": "",
            "bip_url": month_page_url,
        }
        out.append(records)
    return out


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def _clean_radny(raw: str) -> str:
    s = _clean(raw)
    s = re.split(r"[,;&]|\s+oraz\s+", s)[0].strip()
    return s


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i wniosków radnych z BIP Bolesławca"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też starsze kadencje; domyślnie bieżąca (IX, 2024-2029)",
    )
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Bolesławca (IX kadencja) ===")
    kad_html = fetch_text(session, f"{BASE}/?id={KADENCJA_ID}")
    years = _subpages(kad_html)
    if not years:
        print("  Brak podstron lat — sprawdź id kadencji.")
    _log(f"  Lata: {years}")
    min_rok = None if args.all else MIN_ROK_DEFAULT

    records = []
    for yid, yname in years:
        mrok = re.search(r"(20\d{2})", yname)
        rok = int(mrok.group(1)) if mrok else 0
        if min_rok and rok < min_rok:
            _log(f"  Pomijam rok {yname}")
            continue
        yurl = f"{BASE}/?id={yid}"
        yhtml = fetch_text(session, yurl)
        months = _subpages(yhtml)
        _log(f"  {yname}: {len(months)} miesięcy")
        for mid, mname in months:
            murl = f"{BASE}/?id={mid}"
            mhtml = fetch_text(session, murl)
            recs = parse_month(mhtml, rok, murl)
            _log(f"    {mname}: {len(recs)} wpisów")
            records.extend(recs)
            time.sleep(DELAY)

    # dedupe po treści URL (ochrona przed zduplikowanymi linkami w podstronach)
    seen = set()
    uniq = []
    for r in records:
        key = (r["tresc_url"], r["radny"], r["przedmiot"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    records = uniq

    records.sort(key=lambda r: (r["rok"], r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje/wnioski: {interp}")
    print(f"Z odpowiedzią:        {answered}")
    print(f"Razem:                {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
