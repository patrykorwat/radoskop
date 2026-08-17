#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Olsztyna.

Źródło: BIP Olsztyna — kategoria "Interpelacje i zapytania Radnych":

    https://bip.olsztyn.eu/kategoria/46/interpelacje-i-zapytania-radnych.html

Po co: Rada Miasta Olsztyna NIE publikuje interpelacji na eSesja
(esesja_url = null w config.json), tylko na BIP w formie postów podsumowujących
per sesja / per okres międzysesyjny.

Struktura źródła (różni się od CCT z Przemysła):
  * Listing = strona kategorii 46 z paginacją `?page=N`. Każdy wpis to post
    (URL `/{id}/{slug}.html`) o tytule np.:
        "Interpelacje i zapytania radnych zgłoszone 29 kwietnia 2026 r.
         podczas XXV sesji Rady Miasta"
        "Interpelacje radnych złożone w lipcu 2026 - okres międzysesyjny"
  * Treść postu = `<ol>` z `<li>`, każdy element:
        "Imię Nazwisko - w sprawie {temat}"
    Jeden `<li>` = jedna interpelacja/zapytanie.
  * Załączniki (`a.post-attach-link`): PDF treści ("Interpelacja ...") oraz
    ewentualnie "Odpowiedź na interpelację ...". Dopasowujemy je do radnego
    po znormalizowanym nazwisku (prefiks).

Output: lista rekordów w formacie Radoskop (ten sam schemat co Warszawa/
Bydgoszcz/Przemyśl):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego pochodzi z config.json (club_assignments -> clubs). Uwaga: dla
Olsztyna config.json NIE zawiera club_assignments (jest tylko słownik `clubs`
z kolorami, bez pól `name`), więc klub będzie pusty — odnotowane w raporcie.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # też rok < 2024
    python3 scrape_interpelacje.py --output docs/interpelacje.json --pages 3   # test na N stronach
"""

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache, cached_fetch_text  # noqa: E402

# Kategoria BIP "Interpelacje i zapytania Radnych" (paginacja przez ?page=N).
KATEGORIA_URL = (
    "https://bip.olsztyn.eu/kategoria/46/interpelacje-i-zapytania-radnych.html"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

DELAY = 0.5
MAX_PAGES = 120
# Domyślnie tylko bieżąca kadencja (IX, 2024-2029) — Radoskop śledzi wyłącznie ją.
MIN_ROK_DEFAULT = 2024

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    # forma miejska w tytułach międzysesyjnych: "złożone w LIPCU 2026"
    "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10, "listopad": 11,
    "grudzień": 12,
}

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _norm(text: str) -> str:
    """Lowercase + usuń polskie diakrytyki (do porównań dopasowywania)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _load_clubs() -> tuple[dict, dict]:
    """(club_assignments, clubs) z config.json miasta."""
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
    if requests is None:
        return None
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(url: str) -> str:
    """Fetch z politeness delay + retry na przejściowych 403/5xx (cache-aware)."""
    # cached_fetch_text nie ma retry; robimy prostą pętle z własnym try.
    for attempt in range(3):
        try:
            return cached_fetch_text(
                url, session=_session(), headers=HEADERS, timeout=30, delay=DELAY
            )
        except Exception as e:  # noqa: BLE001
            _log(f"  błąd {url}: {e}")
            import time
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing listingu (posty kategorii)
# ---------------------------------------------------------------------------

_POST_RE = re.compile(
    r'href="(/\d+/[^"]*?(?:interpelacj|zapytan)[^"]*?\.html)"', re.I
)


def parse_listing(html: str) -> list[str]:
    if not html:
        return []
    out = []
    for m in _POST_RE.finditer(html):
        u = "https://bip.olsztyn.eu" + m.group(1)
        if u not in out:
            out.append(u)
    return out


def _first_group(pattern: str, html: str, flags: int = re.I) -> str:
    m = re.search(pattern, html, flags)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def parse_title(html: str) -> str:
    return _first_group(r'<h2 class="post-title">(.*?)</h2>', html)


def parse_publication_date(html: str) -> str:
    """'Czas publikacji informacji: DD-MM-RRRR ...' -> RRRR-MM-DD."""
    m = re.search(
        r"Czas publikacji informacji:\s*(?:</?[^>]+>\s*)*(\d{1,2}-\d{1,2}-\d{4})",
        html, re.I,
    )
    if not m:
        return ""
    return normalize_date(m.group(1))


# ---------------------------------------------------------------------------
# Daty z tytułów
# ---------------------------------------------------------------------------

def _parse_date_from_title(title: str, pub_date: str) -> str:
    """Wyciąga datę z tytułu: 'zgłoszone 29 kwietnia 2026 r.' -> 2026-04-29.

    Dla postów międzysesyjnych ('złożone w LIPCU 2026') dzień bierzemy z daty
    publikacji (data nie jest znana dokładnie).
    """
    # pełna data: "DD miesiąca RRRR"
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+?)\s+(\d{4})", title, re.I)
    if m:
        day, month, year = m.groups()
        mp = _MONTHS.get(month.lower())
        if mp:
            return f"{year}-{mp:02d}-{int(day):02d}"
    # miesiąc + rok (międzysesyjny): "w LIPCU 2026"
    m = re.search(r"w\s+([a-ząćęłńóśźż]+?)\s+(\d{4})", title, re.I)
    if m:
        month, year = m.groups()
        mp = _MONTHS.get(month.lower())
        if mp:
            day = ""
            if len(pub_date) == 10 and pub_date.startswith(f"{year}-{mp:02d}-"):
                day = pub_date
            else:
                day = f"{year}-{mp:02d}-01"
            return day
    return pub_date


def normalize_date(dd_mm_yyyy: str) -> str:
    """DD-MM-RRRR -> RRRR-MM-DD (sortowanie chronologiczne w frontendzie)."""
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*", dd_mm_yyyy or "")
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


# ---------------------------------------------------------------------------
# Parsing postu (interpelacje + załączniki)
# ---------------------------------------------------------------------------

_OL_RE = re.compile(r"<ol>(.*?)</ol>", re.S)
_LI_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)

_ATTACH_RE = re.compile(
    r'<a class="post-attach-link[^"]*"\s+href="([^"]+)">\s*(.*?)\s*</a>',
    re.S,
)


def _clean(s: str) -> str:
    import html as _html
    s = _html.unescape(s or "")
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _post_body(html: str) -> str:
    """Region głównej treści postu (div.post-body.cke) — bez menu/boksu."""
    m = re.search(
        r'<div class="post-body\s+cke"[^>]*>(.*?)(?:<!--\s*Attachments|</article>)',
        html, re.S,
    )
    if not m:
        m = re.search(r'<div class="post-body[^>]*>(.*?)</div>\s*<!--\s*Attachments', html, re.S)
    return m.group(1) if m else ""


_W_SPRAWIE_RE = re.compile(r"w\s*[sś][\wąćęłńóśźż]{3,10}ie(?=\s|,|$)", re.I)


def parse_post_items(html: str) -> list[dict]:
    """Elementy `<ol><li>` z treści postu — każdy = jedna interpelacja/zapytanie.

    Format li: "Imię Nazwisko - w sprawie temat" (czasem bez 'w sprawie').
    Parsujemy WYŁĄCZNIE w obrębie div.post-body.cke, żeby nie łapać pozycji
    menu nawigacji (też są w `<li>`).
    """
    body = _post_body(html)
    items = []
    ol = _OL_RE.search(body)
    li_source = ol.group(1) if ol else body
    for m in _LI_ITEM_RE.finditer(li_source):
        text = _clean(m.group(1))
        if not text:
            continue
        # separacja radnego od przedmiotu po "w sp[ra]?awie" (tolerancja literówek
        # BIP: "w spawie", "w sparwie", "w srawie") — pierwsze wystąpienie
        sprawie = _W_SPRAWIE_RE.search(text)
        if sprawie:
            radny = text[: sprawie.start()].rstrip(" -–").strip()
            przedmiot = text[sprawie.end():].strip(" \u00a0:,")
        else:
            radny, przedmiot = text, ""
        # przedmiot może zaczynać się od " : " / dwukropka
        przedmiot = przedmiot.lstrip(" :")
        przedmiot = re.sub(r"\s+", " ", przedmiot)
        if radny and len(radny) >= 3:
            items.append({"radny": radny, "przedmiot": przedmiot})
    return items


def _surname_key(radny: str) -> str:
    """Klucz dopasowania załącznika: prefiks znormalizowanego nazwiska."""
    name = re.sub(r"\s+", " ", radny).strip()
    last = name.split()[-1] if name else ""
    norm = _norm(last).lstrip("-–")
    return norm[:7] if norm else ""


def parse_attachments(html: str) -> list[tuple[str, str]]:
    """Lista (label, href) załączników PDF."""
    out = []
    for href, label in _ATTACH_RE.findall(html):
        out.append((_clean(label), href.strip()))
    return out


def _attachment_for(attachments: list, key: str) -> tuple[str, str]:
    """(tresc_url, odpowiedz_url) dla radnego o podanym kluczu nazwiska."""
    tresc = ""
    odpowiedz = ""
    for label, href in attachments:
        low = _norm(label)
        if key and key not in low:
            continue
        if "odpowied" in low:
            if not odpowiedz:
                odpowiedz = href
        elif "interpelacj" in low or "zapytan" in low:
            if not tresc:
                tresc = href
    return tresc, odpowiedz


def build_records(html: str, bip_url: str, min_rok: int | None) -> list[dict]:
    title = parse_title(html)
    pub_date = parse_publication_date(html)
    data_wplywu = _parse_date_from_title(title, pub_date)
    rok = int(data_wplywu[:4]) if len(data_wplywu) >= 4 else 0

    if min_rok and rok < min_rok:
        return []

    attachments = parse_attachments(html)
    items = parse_post_items(html)

    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"
    records = []
    for i, item in enumerate(items, start=1):
        radny = item["radny"]
        przedmiot = item["przedmiot"] or title
        key = _surname_key(radny)
        tresc_url, odpowiedz_url = _attachment_for(attachments, key)

        # typ: dopasowane załączniki rozstrzygają; inaczej domyślnie interpelacja
        typ = "interpelacja"
        for label, _href in attachments:
            low = _norm(label)
            if key and key in low and "zapytan" in low and "odpowied" not in low:
                typ = "zapytanie"
                break

        cri = f"{rok}-{bip_url.split('/')[3]}-{i}" if len(bip_url.split('/')) > 3 else f"{rok}-{i}"

        records.append({
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
            "bip_url": bip_url,
        })
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Olsztyna"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument(
        "--pages", type=int, default=None,
        help="Ogranicz liczbę stron listingu (do testów); domyślnie wszystkie",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też rok < 2024; domyślnie tylko 2024-2029 (IX kadencja)",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)

    print("=== Interpelacje — BIP Olsztyna ===")
    seen: dict[str, str] = {}  # bip_url -> html
    page = 1
    empty_streak = 0
    max_pages = args.pages or MAX_PAGES
    while page <= max_pages:
        url = f"{KATEGORIA_URL}?page={page}"
        html = fetch_text(url)
        links = parse_listing(html)
        new_links = [u for u in links if u not in seen]
        _log(f"  strona {page}: {len(links)} postów, nowych: {len(new_links)}")
        if not new_links:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        for u in new_links:
            seen[u] = ""
        if page % 10 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)} postów)")
        page += 1
    print(f"  Listing: {len(seen)} postów kategorii (do strony {page - 1})")

    records: list[dict] = []
    fetched = 0
    for url in seen:
        html = fetch_text(url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        recs = build_records(html, url, min_rok)
        fetched += 1
        records.extend(recs)
        if fetched % 20 == 0:
            print(f"  posty: {fetched}...")

    # dedupe
    uniq: dict[tuple, dict] = {}
    for r in records:
        uniq[(r["radny"], r["przedmiot"], r["data_wplywu"])] = r
    records = list(uniq.values())

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
