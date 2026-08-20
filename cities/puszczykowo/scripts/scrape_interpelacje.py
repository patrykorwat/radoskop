#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Puszczykowa (BIP wokiss).

Źródło: bip.puszczykowo.pl (wokiss) — strona Rada Miasta > Interpelacje i
zapytania > Kadencja 2024-2029:

    https://bip.puszczykowo.pl/bip/organy/rada-miasta/interpelacje-i-zapytania/kadencja-2024-2029.html

Struktura: jedna strona, trzy tabele HTML (sekcje wg roku złożenia: 2026, 2025,
2024). Każdy wiersz tabeli = jeden rekord:
    Lp. | Radna/y | Data złożenia | Interpelacja/zapytanie | Odpowiedź
Kolumny "Interpelacja/zapytanie" i "Odpowiedź" to linki PDF ("tekst") albo
tekst/dla brakującej odpowiedzi. Typ (interpelacja/zapytanie) rozpoznajemy z
nazwy pliku PDF treści (np. "...-zapytanie.pdf", "...-interpelacja.pdf").
Przedmiot: załączniki PDF są SKANAMI bez warstwy tekstowej (pypdf textlen=0)
-> przedmiot pozostaje pusty (NIE fabrykowany), source=partial / subject-field
none-reliable; metadane (radny, data złożenia, typ, linki) są realne.

Klub radnego z config.json (club_assignments -> clubs) — dokładne dopasowanie
nazwiska (nazwiska w tabeli są w pełnej poprawnej formie).
Odpowiedź: status "Udzielono" gdy jest link/tekst w kolumnie odpowiedzi;
data_odpowiedzi nie jest publikowana w tabeli (pusta, uczciwa luka).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/x
"""

import argparse
import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

PAGE_URL = (
    "https://bip.puszczykowo.pl/bip/organy/rada-miasta/"
    "interpelacje-i-zapytania/kadencja-2024-2029.html"
)
BASE = "https://bip.puszczykowo.pl/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5


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


def _club_for(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def fetch_text(session, url) -> str:
    for attempt in range(3):
        try:
            import time
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                import time as _t
                _t.sleep(4)
                continue
        except requests.RequestException as e:
            import time as _t
            print(f"  błąd {url}: {e}")
            _t.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _cell_html(td):
    """Zwraca (tekst, hrefs) komórki <td>."""
    txt = _clean(td)
    hrefs = [urljoin(BASE, h) for h in re.findall(r'href="([^"]+)"', td)]
    return txt, hrefs


def _normalize_date(s: str) -> str:
    """DD.MM.RRRR albo D.M.RRRR -> RRRR-MM-DD."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", s or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _typ_from_url(url: str, fallback: str) -> str:
    low = (url or "").lower()
    if "-zapytanie" in low or "_zapytanie" in low or "zapytanie." in low:
        return "zapytanie"
    return "interpelacja"


def parse_page(html: str) -> list[dict]:
    """Parsuje wszystkie trzy tabele w jednej stronie. Zwraca surowe rekordy."""
    out = []
    seen_urls = set()
    # Każdy <table> to sekcja roku; tytuły <h2> niosą rok, ale rok mamy też z daty.
    for tb in re.split(r"<table", html)[1:]:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S | re.I)
        for row in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            if len(tds) < 5:
                continue
            lp, radny, data, interp_td, odp_td = tds[0], tds[1], tds[2], tds[3], tds[4]
            lp_txt = _clean(lp)
            if lp_txt in ("", "Lp.", "\xa0", "\u00a0", "Lp", "Radna/y"):
                continue
            radny_txt = _clean(radny)
            data_str = _clean(data)
            interp_txt, interp_hrefs = _cell_html(interp_td)
            odp_txt, odp_hrefs = _cell_html(odp_td)

            data_iso = _normalize_date(data_str)
            rok = int(data_iso[:4]) if data_iso and data_iso[:4].isdigit() else 0

            # treść: pierwszy link PDF w kolumnie interpelacja/zapytanie
            tresc_url = interp_hrefs[0] if interp_hrefs else ""
            typ = _typ_from_url(tresc_url, "interpelacja")
            # odpowiedź: link PDF w kolumnie odpowiedzi -> Udzielono.
            # Sam tekst (np. "Pismo zostanie rozpatrzone...") to nota o biegu
            # sprawy, NIE faktyczna odpowiedź — traktujemy jako "Nie udzielono".
            odpowiedz_url = odp_hrefs[0] if odp_hrefs else ""
            odpowiedz_status = "Udzielono" if odp_hrefs else "Nie udzielono"

            cri = f"puszczykowo-{rok}-{lp_txt.rstrip('.')}".strip()
            if tresc_url in seen_urls:
                continue  # dedupe
            seen_urls.add(tresc_url)

            out.append({
                "cri": cri,
                "typ": typ,
                "rok": rok,
                "kadencja": "2024-2029" if rok >= 2024 else ("2018-2024" if rok else ""),
                "radny": radny_txt,
                "przedmiot": "",  # skany PDF bez warstwy tekstowej — nie fabrykujemy
                "data_wplywu": data_iso,
                "klub": _club_for(radny_txt),
                "odpowiedz_status": odpowiedz_status,
                "tresc_url": tresc_url,
                "odpowiedz_url": odpowiedz_url,
                "data_odpowiedzi": "",  # tabela nie podaje daty odpowiedzi
                "bip_url": PAGE_URL,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Puszczykowo (BIP wokiss)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args()

    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Puszczykowo (BIP wokiss) ===")
    html = fetch_text(session, PAGE_URL)
    if not html:
        print("  [skip] brak treści strony źródłowej")
        return 1

    records = parse_page(html)

    # Strona gromadzi rokami; posortuj malejąco wg daty, potem cri.
    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_answer = sum(1 for r in records if r["odpowiedz_status"] == "Nie udzielono")
    no_club = sum(1 for r in records if not r["klub"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | z odpowiedzią: {answered} "
          f"| bez odpowiedzi: {no_answer} | bez klubu: {no_club} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
