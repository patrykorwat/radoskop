#!/usr/bin/env python3
"""Radoskop Złotów — interpelacje/zapytania radnych (IX kad. 2024-2029).

Źródło: BIP Złotowa (backend: Next.js React-SPA, platforma Nefeni/BIP.net.pl,
dane w locie `self.__next_f.push` — ten sam CMS co Mikołów).

Struktura różni się od Mikołowa: interpelacje/zapytania są grupowane w JEDEN
artykuł na rok (kategorie pod "Rada Miejska > Interpelacje i zapytania radnych"):
  * 2026: /kategorie/273-.../artykuly/2556-interpelacje-i-zapytania-radnych-2026
  * 2025: /kategorie/240-.../artykuly/1932-...
  * 2024: /kategorie/214-.../artykuly/1142-...
Treść artykułu (pole `content` odwołujące się do wiersza RSC "$N") to tabela
HTML: Lp | Zgłaszający | Treść | Data złożenia | Skan interpelacji | Skan odpowiedzi.
Każdy wiersz = jedna interpelacja/zapytanie z linkami PDF (skany, bez warstwy
tekstowej — przedmiot bierzemy z kolumny Treść).

ŹRÓDŁO CZĘŚCIOWE (source=partial), uczciwie udokumentowane:
  * TYP: rejestr jest łączony "Interpelacje i zapytania radnych" — nie ma
    kolumny typu, a skany PDF (bez warstwy tekstowej) są zbiorcze
    ("interpelacje i zapytania złożone przez radnego X"). Typu per-rekord NIE
    da się wiarygodnie rozdzielić ze źródła → domyślnie "interpelacja"
    (nominalna, dominująca forma rejestru). Nie OCR-ujemy (kontener NAS bez
    tesseract).
  * RADNY: metryczka podaje tylko inicjał+nazwisko ("Radny K. Koronkiewicz").
    Dopasowujemy przez nazwisko do club_assignments z config.json (unikalne
    nazwisko -> pełne imię; np. K.→Krzysztof Koronkiewicz). Radni spoza
    club_assignments (Głyżewski, Golla, Zając, Masternak) zostają z samym
    nazwiskiem + klub="" (uczciwie, jak Zgorzelec).
  * data_odpowiedzi: brak w źródle (skany odpowiedzi bez daty) -> "".

Klub z config.json (club_assignments -> clubs). Autor zbiorowy/kolektyw: n/d.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
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

BASE = "https://bip.zlotow.pl"

# Rok -> (kategoria-id, artykul-id). Kategorie pod "Interpelacje i zapytania radnych".
YEARS = {
    "2024": (214, 1142),
    "2025": (240, 1932),
    "2026": (273, 2556),
}
KADENCJA = "2024-2029"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
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

def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and "<!doctype html".encode() in resp.text.encode().lower():
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429, 500, 502, 503):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Next.js RSC flight-data
# ---------------------------------------------------------------------------

_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[\d+,"((?:[^"\\]|\\.)*)"\]\)')


def flight_buffer(html):
    out = []
    for m in _FLIGHT_RE.finditer(html or ""):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            continue
    return "\n".join(out)


_ATT_RE = re.compile(r'tresc="([^"]+)"\s+odp="([^"]*)"')
_TR_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')


def _clean(cell: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell))).strip()


def _content_segment(buf: str) -> str:
    """Zawartość HTML artykułu (tabela interpelacji).

    Rejestr Złotowa to tabela w treści artykułu. Identyfikatory wierszy RSC
    (\"content\":\"$N\") bywają NIESTABILNE między requestami, więc nie polegamy
    na numerze wiersza — szukamy tabeli zawierającej nagłówek
    \"Zgłaszający interpelację/zapytanie\". Tabela bywa w buforku zduplikowana;
    deduplikację robimy w main() po tresc_url.
    """
    tables = []
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", buf, re.S):
        block = m.group(0)
        if "Zgłaszający" in block or "Zgłaszajacy" in block:
            tables.append(block)
    return "\n".join(tables)


def _parse_rows(seg: str):
    """Wiersze tabeli: (lp, radny, tresc, data, tresc_url, odpowiedz_url)."""
    out = []
    for tr in _TR_ROW_RE.findall(seg):
        tds = _TD_RE.findall(tr)
        txt = [_clean(c) for c in tds]
        href = [_HREF_RE.findall(c) for c in tds]
        if not txt or not re.search(r"\d", txt[0] or ""):
            continue
        lp, radny, tresc = (txt + ["", "", ""])[:3]
        data = txt[3] if len(txt) > 3 else ""
        tresc_url = (href[4][0] if len(href) > 4 and href[4] else "")
        odp_url = (href[5][0] if len(href) > 5 and href[5] else "")
        out.append((lp, radny, tresc, data, tresc_url, odp_url))
    return out


# --- radny: inicjał+nazwisko -> pełne imię z config (unikalne nazwisko) ---

def _radny_normalized(raw: str) -> str:
    """'Radny K. Koronkiewicz' / 'Radna M. Wegner' -> pełny klucz config lub nazwisko."""
    if not raw:
        return ""
    # usuń prefiks Radny/Radna
    body = re.sub(r"^(?:Radn[ya]|radn[ya])[\s:]+", "", raw).strip()
    # inicjał (1-2 litery + kropka ew.) na początku
    body = re.sub(r"^[A-ZĄĆĘŁŃÓŚŹŻ]\.?\s*", "", body).strip()
    surname = body
    # dopasowanie po pełnym nazwisku (ignorując polskie znaki)
    norm = lambda s: re.sub(r"[^\w]", "", s).lower()
    target = norm(surname)
    if not target:
        return ""
    hits = [name for name in _CLUB_ASSIGN if norm(name.split()[-1]) == target]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # więcej radnych o tym samym nazwisku -> niejednoznaczne, zostaw nazwisko
        return surname
    return surname  # spoza club_assignments -> samo nazwisko, klub ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Złotowa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-years", type=int, default=len(YEARS), help="Max lat (1-3)")
    args = parser.parse_args()

    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    seen: dict = {}
    rows = []
    for year in sorted(YEARS.keys(), reverse=True):
        url = _article_url(year)
        time.sleep(DELAY)
        html = fetch_text(session, url)
        seg = _content_segment(flight_buffer(html))
        year_rows = _parse_rows(seg)
        print(f"[{year}] wierszy tabeli: {len(year_rows)}")
        rows.extend(year_rows)

    # dedupe po tresc_url: rejestr duplikuje tabelę w locie RSC oraz łączy
    # wiele LP w jeden skan-dokument -> jeden rekord na dokument.
    seen_url = {}
    for (lp, radny, tresc, data, tu, ou) in rows:
        if not tu or tu in seen_url:
            continue
        seen_url[tu] = True
        rec = _build_rec(lp, radny, tresc, data, tu, ou)
        if rec:
            seen[tu] = rec

    records = [r for r in seen.values() if (MIN_ROK_DEFAULT is None or r["rok"] >= MIN_ROK_DEFAULT)]
    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    novlos = [r["radny"] for r in records if not r["radny"]]
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {sum(1 for r in records if r['typ'] == 'zapytanie')}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")
    if novlos:
        print(f"Bez radnego (spoza config/niejednoznaczne): {len(novlos)}  {novlos[:12]}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


def _article_url(year: str) -> str:
    cat_id, art_id = YEARS[year]
    return f"{BASE}/kategorie/{cat_id}-interpelacje-i-zapytania-radnych-z-{year}-r/artykuly/{art_id}-interpelacje-i-zapytania-radnych-{year}?lang=PL"


def _build_rec(lp, radny, tresc, data, tu, ou):
    radny_full = _radny_normalized(radny)
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})", data or "")
    data_wplywu = ""
    rok = 0
    if m:
        d, mo, y = m.groups()
        data_wplywu = f"{y}-{int(mo):02d}-{int(d):02d}"
        rok = int(y)
    # cri: stabilny identyfikator = numer dokumentu z nazwy pliku treści
    stem = (tu or "").rstrip("/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(pdf|PDF)$", "", stem)
    # numer dokumentu (ciąg literowo-cyfrowy z dywizami) albo hash z URL
    cri = re.sub(r"[^A-Za-z0-9]", "", stem)[:48] or \
        ("zlotow" + re.sub(r"[^0-9]", "", tu or "")[:32])
    przedmiot = tresc.strip()
    return {
        "cri": cri,
        "typ": "interpelacja",  # źródło łączone, typ nierozdzielny -> nominalna (patrz docstring)
        "rok": rok,
        "kadencja": KADENCJA,
        "radny": radny_full,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny_full),
        "odpowiedz_status": "Udzielono" if ou else "Nie udzielono",
        "tresc_url": tu,
        "odpowiedz_url": ou,
        "data_odpowiedzi": "",
        "bip_url": tu or _article_url(str(rok)),
    }


if __name__ == "__main__":
    raise SystemExit(main())
