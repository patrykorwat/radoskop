#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Brodnicy.

Źródło: BIP Brodnicy — rejestr "Interpelacje i zapytania radnych" (tabela).

    https://bip.brodnica.pl/index.php?type=4&name=bt16X&func=selectsite&value[0]=mnu8&value[1]=YY

eSesja (https://brodnica.esesja.pl) — moduł interpelacje "Brak aktywności lub
moduł nieaktywny" (uczciwa luka), dlatego źródłem jest rejestr na BIP.

Struktura:
  Każdy rok kadencji 2024-2029 ma osobny podrejestr (src=page_per_year):
    2026 -> bt163 / value[1]=95
    2025 -> bt164 / value[1]=91
    2024 -> bt165 / value[1]=84
  To pojedyncza tabela (bez paginacji) z kolumnami:
    Lp. | Data wpływu | Znak sprawy | Zgłaszający | Przedmiot interpelacji/zapytania | Odpowiedź
  Kolumna "Przedmiot" zawiera link PDF (treść interpelacji), kolumna
  "Odpowiedź" link(i) PDF (odpowiedź urzędu).

Rejestr łączy interpelacje i zapytania (nagłówek kolumny "Przedmiot
interpelacji/zapytania"). typ: "zapytanie" gdy przedmiot wprost mówi o
zapytaniu, w przeciwnym razie "interpelacja" (rejestr zdominowany przez
interpelacje; brak osobnego podziału w źródle).

Klub radnego z config.json (club_assignments -> clubs). Zgłaszający w
rejestrze to "X. Nazwisko" — rozwijane do pełnego imienia z club_assignments.

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE_URL = "https://bip.brodnica.pl"

# strona_rejestru: rok -> (bt_id, value1)
PAGE_PER_YEAR = {
    2026: ("bt163", "95"),
    2025: ("bt164", "91"),
    2024: ("bt165", "84"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.6
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029
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


def _clean_radny(raw: str) -> str:
    """Rozwija 'X. Nazwisko' do pełnego imienia z config club_assignments.

    Dopasowuje po nazwisku (ostatni token); gdy kilka radnych o tym samym
    nazwisku — po inicjale imienia. Zwraca wpis config (spójność nazw i klubu),
    a gdy nie ma dopasowania — oryginalny tekst rejestru (klub zostanie pusty).
    """
    s = re.sub(r"\s+", " ", raw or "").strip().rstrip(".")
    if not s:
        return ""
    if s in _CLUB_ASSIGN:
        return s
    # 'X. Nazwisko' / 'Nazwisko' / 'Imię Nazwisko'
    tokens = s.split()
    if not tokens:
        return ""
    surname = tokens[-1].lower().rstrip(".")
    initial = ""
    if len(tokens) >= 2 and re.match(r"^[a-ząęóśłżźćń]$", tokens[0].lower().rstrip(".")):
        initial = tokens[0].lower().rstrip(".")

    best, bestscore = "", 0.0
    for cname in _CLUB_ASSIGN:
        ct = cname.split()
        if not ct:
            continue
        score = SequenceMatcher(None, ct[-1].lower(), surname).ratio()
        if score < 0.7:
            continue
        if score > bestscore:
            best, bestscore = cname, score
        elif score == bestscore and best:
            # remis — sprawdź inicjał imienia
            cfirst = ct[0][0].lower()
            if initial and cfirst == initial:
                best = cname
    if best and bestscore >= 0.7:
        # potwierdź inicjałem, jeśli podany i występują remisy
        return best
    # gdy bezpośrednie dopasowanie nazwiska — przyjmij (pojedynczy kandydat)
    return s


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
            time.sleep(DELAY)
            resp = session.get(url, timeout=30, verify=False)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(4)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


_PDF_URL_RE = re.compile(r"^(https?://|/)")


def _resolve(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE_URL + href


def _dt_from_pl(date_str: str) -> str:
    """'7.1.2025r.' -> '2025-01-07' (DD.MM.YYYY). Zwraca '' gdy nie uda się."""
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](20\d{2})", date_str or "")
    if not m:
        return ""
    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except Exception:
        return ""


def parse_table(html: str, bip_url: str) -> list[dict]:
    """Parsuje tabelę rejestru (kolumny jak w nagłówku)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr")
    out = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        lp = tds[0].get_text(" ", strip=True)
        if not re.match(r"^\d", lp):
            continue  # nagłówek
        data_raw = tds[1].get_text(" ", strip=True)
        cri = tds[2].get_text(" ", strip=True).strip()
        zglaszający = tds[3].get_text(" ", strip=True)
        # przedmiot: tekst z komórki; tresc_url = pierwszy href pdf
        przedmiot_td = tds[4]
        przedmiot = re.sub(r"\s+", " ", przedmiot_td.get_text(" ", strip=True)).strip()
        # usuń artefakt rozmiaru załącznika, np. "(pdf,11.96MB)"
        przedmiot = re.sub(
            r"\s*\((?:pdf|docx?|odt|rtf|jpg|jpeg|png|zip|rar|xlsx?)\s*,\s*[\d.,]+\s*(?:MB|kB|KB)(?:yte)?\)",
            "", przedmiot, flags=re.I,
        ).strip()
        przedmiot = re.sub(r"\s+", " ", przedmiot).strip()
        tresc_url = ""
        a = przedmiot_td.find("a", href=True)
        if a:
            tresc_url = _resolve(a["href"])
        # odpowiedź: urzędu PDF-y
        odp_td = tds[5]
        odpowiedz_url = ""
        a2 = odp_td.find("a", href=True)
        if a2:
            odpowiedz_url = _resolve(a2["href"])

        if not cri and not przedmiot:
            continue

        data_wplywu = _dt_from_pl(data_raw)
        rok = 0
        mrok = re.search(r"(20\d{2})", cri or "")
        if mrok:
            rok = int(mrok.group(1))
        if not rok and data_wplywu:
            rok = int(data_wplywu[:4])

        radny = _clean_radny(zglaszający)
        low = przedmiot.lower()
        if "zapytan" in low:
            typ = "zapytanie"
        else:
            typ = "interpelacja"

        odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

        out.append({
            "cri": cri,
            "typ": typ,
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "2018-2023",
            "radny": radny,
            "przedmiot": przedmiot,
            "data_wplywu": data_wplywu,
            "klub": _club_for_radny(radny),
            "odpowiedz_status": odpowiedz_status,
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": "",
            "bip_url": bip_url,
        })
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Brodnicy"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też starsze kadencje (sprzed 2024); domyślnie bieżąca",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Brodnicy ===")
    records = []
    seen_cri = set()
    years = sorted(PAGE_PER_YEAR, reverse=True)
    for year in years:
        bt, v1 = PAGE_PER_YEAR[year]
        url = (f"{BASE_URL}/index.php?type=4&name={bt}&func=selectsite"
               f"&value%5B0%5D=mnu8&value%5B1%5D={v1}")
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        recs = parse_table(html, url)
        new = [r for r in recs if r["cri"] not in seen_cri]
        for r in new:
            seen_cri.add(r["cri"])
        print(f"  rok {year}: {len(recs)} wierszy ({len(new)} nowych)")
        records.extend(new)

    if min_rok:
        records = [r for r in records if not r["rok"] or r["rok"] >= min_rok]

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
