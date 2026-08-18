#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Legionowa.

Źródło: BIP Legionowa (https://bip.legionowo.pl) — Madkom "playout" React-SPA.
Wszystkie ścieżki zwracają ten sam ~4KB shell index.html; treść dostępna jest
wyłącznie przez JSON API pod `/api/`. Rejestr interpelacji NIE MA dedykowanego
filtru po menu (parametr `query`/`menu` jest ignorowany), więc skanujemy cały
indeks artykułów (`/api/contexts/default/articles`, ~10 000 pozycji) i
wybieramy te, których kategoria menu to `Interpelacje, zapytania` /
`Interpelacje` / `Zapytania` / `Interpelacje i zapytania` albo tytuł zawiera
"interpelacj"/"zapytan".

    https://bip.legionowo.pl/api/contexts/default/articles?limit=200&offset=N
    https://bip.legionowo.pl/api/articles/{id}

eSesja (https://legionowo.esesja.pl/interpelacje_i_zapytania) — moduł
NIEAKTYWNY dla bieżącej kadencji (strona pokazuje tylko zakładki kadencji
2014-2018 i 2018-2023, brak 2024-2029; tekst "moduł nieaktywny"), dlatego
źródłem danych jest rejestr na BIP.

Detal artykułu (JSON) zawiera pole `content` (czysty tekst wystąpienia wraz z
odpowiedzią w tym samym dokumencie):
  * "złożona NN <miesiąc> RRRR roku"              -> data_wplywu
  * sygnatariusz przed "Prezydent ..." (radny)    -> radny
  * "W odpowiedzi na..."/"Odpowiadając" + data    -> odpowiedz_status / data_odpowiedzi
  * tytuł zaczyna się od "Interpelacja"/"Zapytanie" -> typ

Klub radnego z config.json (club_assignments -> clubs). Część bieżących radnych
(Paweł Głażewski, Agnieszka Flak, ...) nie figuruje w club_assignments — dla
nich `radny` bierzemy z tekstu, ale `klub` pozostaje pusty (brak przypisania w
konfiguracji do uzupełnienia). Interpelacje zbiorowe bez podanych nazwisk
(anonymizowane) mają pusty `radny`.

Output: rekordy w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/interp/legionowo
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # wcześniejsze kadencje
"""

import argparse
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.legionowo.pl"
ARTICLES_API = f"{BASE}/api/contexts/default/articles"
DETAIL_API = f"{BASE}/api/articles"
PAGE_LIMIT = 200
MAX_INDEX = 10000  # indeks BIP ma ~10 000 artykułów (brak filtra po menu)
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.2
_DEBUG = False
_CACHE_DIR = None

# Kategorie-menu z rejestrem interpelacji (różniły się w różnych kadencjach).
_REGISTER_MENUS = {
    "Interpelacje, zapytania",
    "Interpelacje",
    "Zapytania",
    "Interpelacje i zapytania",
}

_CLUB_ASSIGN = {}
_CLUBS = {}

# Miesiące (mianownik i dopełniacz, przez znormalizowane diakrytyki -> numer)
_MONTHS = {
    "styczen": 1, "stycznia": 1, "luty": 2, "lutego": 2, "marzec": 3, "marca": 3,
    "kwiecien": 4, "kwietnia": 4, "maj": 5, "maja": 5, "czerwiec": 6, "czerwca": 6,
    "lipiec": 7, "lipca": 7, "sierpien": 8, "sierpnia": 8, "wrzesien": 9,
    "wrzesnia": 9, "pazdziernik": 10, "pazdziernika": 10, "listopad": 11,
    "listopada": 11, "grudzien": 12, "grudnia": 12,
}


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


# ---------------------------------------------------------------------------
# Cache helpers (JSON, klucz = md5(url+params)) z retry na flaky 5xx
# ---------------------------------------------------------------------------

def _cache_file(url: str, params: dict | None = None) -> Path | None:
    if not _CACHE_DIR:
        return None
    key = url
    if params:
        key += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.json"


def fetch_json(session: requests.Session, url: str, params: dict | None = None):
    """GET z cache (gdy --cache-dir) + retry na błędy 5xx / flake."""
    cf = _cache_file(url, params)
    if cf and cf.exists() and cf.stat().st_size > 20:
        try:
            j = json.loads(cf.read_text(encoding="utf-8"))
            if isinstance(j, (dict, list)):
                return j
        except Exception:
            pass
    data = None
    for attempt in range(6):
        try:
            time.sleep(DELAY)
            resp = session.get(url, params=params, timeout=60)
            if resp.status_code != 200:
                _log(f"  {resp.status_code} {url} {params}")
            else:
                j = resp.json()
                # Odporność: strona może na flaku oddać JSON-string zamiast
                # obiektu — wtedy traktujemy to jak błąd i ponawiamy.
                if isinstance(j, (dict, list)):
                    data = j
                    break
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
        except ValueError:
            _log(f"  zły JSON {url}")
        time.sleep(1 + attempt)
    if data is not None and cf:
        try:
            cf.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
    return data


def discover_index(session: requests.Session) -> list[dict]:
    """Przeskanuj cały indeks artykułów, zwróć kandydatów na interpelacje.

    Serwer bywa płochliwy (losowe 5xx na pojedynczych offsetach). Każda
    udana strona zapisywana jest do cache (--cache-dir), więc powtarzane
    uruchomienia pomijają już pobrane offset-y i dogrywają brakujące. Pętla
    wykonuje kolejne przebiegi, aż wszystkie offset-y (0..9800 co 200) zostaną
    pomyślnie pobrane — to gwarantuje kompletność rejestru.
    """
    offsets = list(range(0, MAX_INDEX, PAGE_LIMIT))
    done = set()
    candidates = []
    seen_ids = set()

    def process(offset: int, d: dict):
        for e in (d.get("elements") or []):
            menu = (e.get("menu") or {}).get("name", "") or ""
            title = e.get("title", "") or ""
            title_l = title.lower()
            hay = (menu + " " + title).lower()
            # Pomijamy procedury zamówień publicznych ("zapytanie ofertowe")
            # i osobne artykuły-odpowiedzi (odpowiedź jest włączona do
            # artykułu interpelacji w składzie łączonym).
            if "ofertow" in hay:
                continue
            if title_l.startswith("odpowiedź na interpelacj") or title_l.startswith("odpowiedź na zapytanie"):
                continue
            is_register = menu in _REGISTER_MENUS
            is_interp = bool(re.search(r"interpelacj|zapytan", hay))
            if (is_register or is_interp) and e["id"] not in seen_ids:
                seen_ids.add(e["id"])
                candidates.append({
                    "id": e["id"],
                    "link": e.get("link", ""),
                    "title": title,
                    "menu": menu,
                })

    # Przebiegi aż do skompletowania wszystkich stron indeksu.
    for sweep in range(20):
        progressed = False
        for offset in offsets:
            if offset in done:
                continue
            d = fetch_json(session, ARTICLES_API, {"limit": PAGE_LIMIT, "offset": offset})
            if d is None:
                # twarde desperackie ponowienie tej strony
                for extra in range(6):
                    time.sleep(3 + extra * 2)
                    d = fetch_json(session, ARTICLES_API,
                                   {"limit": PAGE_LIMIT, "offset": offset})
                    if d is not None:
                        break
            if d is not None:
                process(offset, d)
                done.add(offset)
                progressed = True
        if len(done) >= len(offsets):
            break
        if not progressed:
            missing = len(offsets) - len(done)
            print(f"  [warn] przebieg {sweep + 1}: nadal brakuje {missing} stron indeksu")
    return candidates


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    return (s.replace("ą", "a").replace("ę", "e").replace("ó", "o")
             .replace("ś", "s").replace("ł", "l").replace("ż", "z")
             .replace("ź", "z").replace("ń", "n").replace("ć", "c"))


def _parse_polish_date(s: str) -> str:
    """'złożona 11 lutego 2026 roku' / '30.07.2024' -> RRRR-MM-DD."""
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})\s*roku", s, re.I)
    if m:
        d, mo, y = m.group(1), _norm(m.group(2).lower()), m.group(3)
        if mo in _MONTHS:
            return f"{y}-{_MONTHS[mo]:02d}-{int(d):02d}"
    m = re.search(r"\b(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})\b", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


_NAME_RE = re.compile(
    r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:[- ][A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*)"
    r"\s+(?:Radn[ay])\s+", re.U
)


def _extract_radny(c: str) -> str:
    """Imię Nazwisko radnego z bloku podpisu (przed adresatem 'Prezydent')."""
    m = re.search(r"\bPrezydent\b", c, re.I)
    seg = c[: m.start()] if m else c[:2000]
    # Pomijamy fragment wprowadzający (tytuł + 'złożona ...').
    if "złożon" in seg:
        seg = seg.split("złożon")[1]
    # 1) dopasowanie wprost do nazwisk z config (radni bieżącej kadencji)
    best, pos = "", 10**9
    for name in _CLUB_ASSIGN:
        i = seg.find(name)
        if i != -1 and i < pos:
            pos, best = i, name
    if best:
        return best
    # 2) regex na 'NN Nazwisko Radna/Radny (Rady) Miasta'
    m2 = _NAME_RE.search(seg)
    return m2.group(1).strip() if m2 else ""


def _extract_radny_segment_only(c: str) -> str:
    """Fallback: nazwa przed 'Radna/Radny' w całym dokumencie."""
    m = _NAME_RE.search(c)
    return m.group(1).strip() if m else ""


def _parse_detail(d: dict) -> dict | None:
    if not d:
        return None
    content = _text(d.get("content", ""))
    link = d.get("link", "")
    bip_url = f"{BASE}/{link}" if link else f"{BASE}/"

    # Typ wystąpienia
    probe = (d.get("title", "") + " " + content[:300]).lower()
    if "wniosek" in probe:
        typ = "wniosek"
    elif "zapytan" in probe:
        typ = "zapytanie"
    else:
        typ = "interpelacja"

    przedmiot = re.sub(r"\s*—?\s*wraz\s+z\s+odpowiedzi[ąa].*$", "", d.get("title", "")).strip()
    if not przedmiot:
        przedmiot = d.get("title", "")

    data_wplywu = _parse_polish_date(content)

    rok = 0
    try:
        rok = int(data_wplywu[:4]) if data_wplywu else 0
    except ValueError:
        rok = 0

    radny = _extract_radny(content)
    if not radny:
        radny = _extract_radny_segment_only(content)
    klub = _club_for_radny(radny)

    # Odpowiedź
    c_lower = content.lower()
    answered = ("odpowiedzi" in c_lower or "odpowiadając" in c_lower
                or "z odpowiedzią" in c_lower)
    odpowiedz_status = "Udzielono" if answered else "Nie udzielono"
    data_odpowiedzi = ""
    if answered:
        # w sekcji odpowiedzi szukamy daty 'Legionowo, dnia NN <miesiąc> RRRR'
        m = re.search(
            r"odpowiedzi[^.]{0,60}?(?:dnia\s+)?(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})",
            content, re.I)
        if m:
            dd, mo, y = m.group(1), _norm(m.group(2).lower()), m.group(3)
            if mo in _MONTHS:
                data_odpowiedzi = f"{y}-{_MONTHS[mo]:02d}-{int(dd):02d}"
        if not data_odpowiedzi:
            m2 = re.search(
                r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})\b", content)
            if m2:
                data_odpowiedzi = _parse_polish_date(content)

    # cri: id artykułu (unikalny w obrębie rejestru)
    cri = str(d.get("id", ""))

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "",
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": klub,
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": bip_url,
        "odpowiedz_url": bip_url if answered else "",
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": bip_url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG, _CACHE_DIR
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Legionowa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też wcześniejsze kadencje; domyślnie tylko 2024-2029",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    _CACHE_DIR = Path(args.cache_dir) if args.cache_dir else None
    _load_clubs()
    session = _session()

    print("=== Interpelacje / Zapytania — BIP Legionowo ===")
    candidates = discover_index(session)
    print(f"  Indeks: {len(candidates)} kandydatów na interpelacje")

    records = []
    for cand in candidates:
        detail = fetch_json(session, f"{DETAIL_API}/{cand['id']}")
        if not isinstance(detail, dict) or not detail:
            print(f"  [skip] brak treści: {cand['id']}")
            continue
        rec = _parse_detail(detail)
        if not rec:
            continue
        if min_rok and (not rec["rok"] or rec["rok"] < min_rok):
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    with_radny = sum(1 for r in records if r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:   {interp}")
    print(f"Zapytania:      {zap}")
    print(f"Z odpowiedzią:  {answered}")
    print(f"Z radnym:       {with_radny}")
    print(f"Razem:          {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
