#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Ciechocinku.

Źródło: BIP Ciechocinka (mojregion.info) — rejestr "Interpelacje radnych".

    2024-2029: https://mst-ciechocinek.rbip.mojregion.info/1197/interpelacje-radnych-kadencji-2024-2029.html
    2018-2023: https://mst-ciechocinek.rbip.mojregion.info/147/interpelacje-radnych-kadencji-2018-2023.html

eSesja (https://ciechocinek.esesja.pl) — moduł interpelacje nieaktywny, dlatego
źródłem jest rejestr na BIP (tabela, serwer-rendered mojregion.info CMS).

Struktura:
  Pojedyncza tabela na stronę rejestru (bez paginacji w tej chwili) z kolumnami:
    Lp. | Wnioskodawca | Data wpływu | Data przekazania Burmistrzowi |
        | Interpelacja/zapytanie (link PDF treści) | Odpowiedź (link PDF)
  Wnioskodawca to pełne imię i nazwisko radnego. "Interpelacja/zapytanie" zawiera
  link PDF (treść interpelacji) oraz temat ("dot. ...").

Rejestr nosi nazwę "Interpelacje radnych", ale kolumna "Interpelacja/zapytanie"
może zawierać zapytania — typ wyznaczamy po słowie "zapytan" w przedmiocie; w
przeciwnym razie "interpelacja" (rejestr zdominowany przez interpelacje).

Klub radnego z config.json (club_assignments -> clubs).

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także 2018-2023
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

BASE_URL = "https://mst-ciechocinek.rbip.mojregion.info"

# (url, kadencja, min_rok) — kolejnosc: aktualna kadencja na poczatku
REGISTERS = [
    (
        "https://mst-ciechocinek.rbip.mojregion.info/1197/interpelacje-radnych-kadencji-2024-2029.html",
        "2024-2029",
        2024,
    ),
    (
        "https://mst-ciechocinek.rbip.mojregion.info/147/interpelacje-radnych-kadencji-2018-2023.html",
        "2018-2023",
        2018,
    ),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.7
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
    """Normalizuje imię i nazwisko do wpisu z config club_assignments (spójność
    nazw i klubu). Dopasowuje po nazwisku (ostatni token); gdy brak dopasowania —
    zwraca oryginał (klub zostanie pusty)."""
    s = re.sub(r"\s+", " ", raw or "").strip().rstrip(".")
    if not s:
        return ""
    if s in _CLUB_ASSIGN:
        return s
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
            cfirst = ct[0][0].lower()
            if initial and cfirst == initial:
                best = cname
    if best and bestscore >= 0.7:
        return best
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
            resp = session.get(url, timeout=30)
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


_SIZE_RE = re.compile(
    r"\s*\((?:pdf|docx?|odt|rtf|jpg|jpeg|png|zip|rar|xlsx?)\s*,\s*[\d.,]+\s*(?:MB|kB|KB)(?:yte)?\)",
    re.I,
)


def _resolve(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE_URL + href


def _dt_from_pl(date_str: str) -> str:
    """'11.06.2024r.' -> '2024-06-11'. Zwraca '' gdy się nie da."""
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](20\d{2})", date_str or "")
    if not m:
        return ""
    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{yy:04d}-{mm:02d}-{dd:02d}"


def parse_table(html: str, bip_url: str, kadencja: str, min_rok: int) -> list[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr")
    out = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        cri = tds[0].get_text(" ", strip=True).strip()
        if not re.match(r"^\d", cri):
            continue  # nagłówek
        radny_raw = tds[1].get_text(" ", strip=True)
        data_raw = tds[2].get_text(" ", strip=True)

        przedmiot_td = tds[4]
        przedmiot = re.sub(r"\s+", " ", przedmiot_td.get_text(" ", strip=True)).strip()
        przedmiot = _SIZE_RE.sub("", przedmiot).strip()
        przedmiot = re.sub(r"\s+", " ", przedmiot).strip()

        tresc_url = ""
        a = przedmiot_td.find("a", href=True)
        if a:
            tresc_url = _resolve(a["href"])

        odp_td = tds[5]
        odpowiedz_url = ""
        a2 = odp_td.find("a", href=True)
        if a2:
            odpowiedz_url = _resolve(a2["href"])

        if not cri and not przedmiot:
            continue

        data_wplywu = _dt_from_pl(data_raw)
        rok = min_rok
        mrok = re.search(r"(20\d{2})", cri)
        if mrok:
            rok = int(mrok.group(1))
        if data_wplywu and not mrok:
            rok = int(data_wplywu[:4])

        radny = _clean_radny(radny_raw)
        low = przedmiot.lower()
        typ = "zapytanie" if "zapytan" in low else "interpelacja"
        odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

        out.append({
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
        })
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Ciechocinka"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też kadencję 2018-2023; domyślnie bieżąca",
    )
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Ciechocinka ===")
    records = []
    seen = set()
    for url, kadencja, min_rok in REGISTERS:
        if args.all is False and min_rok < MIN_ROK_DEFAULT:
            continue
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        recs = parse_table(html, url, kadencja, min_rok)
        new = [r for r in recs if (r["cri"], r["kadencja"]) not in seen]
        for r in new:
            seen.add((r["cri"], r["kadencja"]))
        print(f"  {kadencja}: {len(recs)} wierszy ({len(new)} nowych)")
        records.extend(new)

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
