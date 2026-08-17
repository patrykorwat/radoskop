#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Rzeszowa.

Źródło: BIP Rzeszowa — rejestr "Interpelacje i zapytania Radnych" IX kadencji
(2024-2029):

    https://bip.erzeszow.pl/3633-ix-kadencja-rady-miasta-rzeszowa-2024-2029/99282-interpelacje-i-zapytania-radnych.html
    (paginacja: ?strona=N)

Struktura:
  * Listing = strona BIP z elementami <li ... class="ak_N" data-id="ID">, tytuł
    w tekście (zawiera "dotyczy..." przedmiot, czasem ucięty końcówką " WIĘCEJ").
  * Szczegóły = strona z H3 typu:
        "Interpelacja nr 70/2026 z dnia 3 sierpnia 2026 r. Pana Rafała Kuliga"
    oraz załącznikami PDF: treść i "Odpowiedź na interpelację..." (obecność
    odpowiedzi => odpowiedz_status "Udzielono").

Autor na BIP jest w DOPEŁNIACZU ("Pana Rafała Kuliga"). Normalizujemy do
mianownika przez dopasowanie do listy radnych z config.json (club_assignments),
a przedmiot bierzemy z "dotyczy ..." (ze szczegółów, w razie potrzeby z listingu).

Output: rekordy w kanonicznym schemacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}
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

REJESTR_URL = (
    "https://bip.erzeszow.pl/3633-ix-kadencja-rady-miasta-rzeszowa-2024-2029/"
    "99282-interpelacje-i-zapytania-radnych.html"
)
BIP_BASE = "https://bip.erzeszow.pl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    # BIP wymaga Referer przy requestach paginowanych (?strona=N) — bez niego
    # serwer oddaje zawsze stronę 1 (dedupe nie widzi nowych stron).
    "Referer": REJESTR_URL,
}

DELAY = 0.5
MAX_PAGES = 60
MIN_ROK_DEFAULT = 2024

_MIESIACE = {
    "styczeń": "01", "stycznia": "01", "luty": "02", "lutego": "02",
    "marzec": "03", "marca": "03", "kwiecień": "04", "kwietnia": "04",
    "maj": "05", "maja": "05", "czerwiec": "06", "czerwca": "06",
    "lipiec": "07", "lipca": "07", "sierpień": "08", "sierpnia": "08",
    "wrzesień": "09", "września": "09", "wrzesnia": "09",
    "październik": "10", "października": "10", "pazdziernika": "10",
    "listopad": "11", "listopada": "11", "grudzień": "12", "grudnia": "12",
}

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


def _fold(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _tokpf(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x == y:
            n += 1
        else:
            break
    return n


def _resolve_radny(gen_form: str) -> str:
    """Dopełniacz z BIP ('Rafała Kuliga'/'Wróbla'/'Woźniak-Kunickiej') ->
    mianownik z config ('Rafał Kulig'). Dopasowanie per-token po wspólnym
    prefiksie (odporne na nieregularne odmiany, np. Wróbel/Wróbla)."""
    gtoks = [_fold(t) for t in re.split(r"\s+", gen_form.strip()) if t]
    if not gtoks:
        return ""
    best, best_score = "", 0
    for cand in _CLUB_ASSIGN.keys():
        ctoks = [_fold(t) for t in cand.split() if t]
        if len(ctoks) < 2:
            continue
        score, ok = 0, True
        for ct in ctoks:
            mt = max((_tokpf(ct, g) for g in gtoks), default=0)
            if mt < max(3, len(ct) - 2):
                ok = False
                break
            score += mt
        if ok and score > best_score:
            best, best_score = cand, score
    if best_score >= 6:
        return best
    return gen_form


_KADENCJA = {str(y): "2024-2029" for y in range(2024, 2030)}


def normalize_date(pl_text: str) -> str:
    """'3 sierpnia 2026' -> '2026-08-03'."""
    m = re.search(
        r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", pl_text or "", re.I
    )
    if not m:
        return ""
    d, mies, y = m.groups()
    mm = _MIESIACE.get(mies.lower())
    if mm is None:
        return ""
    return f"{y}-{mm}-{int(d):02d}"


def parse_detail(html: str, bip_url: str, listing_title: str = "") -> dict | None:
    if not html:
        return None
    h = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    h = re.sub(r"<script.*?</script>", "", h, flags=re.S)
    h = re.sub(r"<style.*?</style>", "", h, flags=re.S)

    title_m = re.search(r"<h3[^>]*>(.*?)</h3>", h, re.S)
    if not title_m:
        return None
    title = re.sub(r"<[^>]+>", " ", title_m.group(1))
    title = re.sub(r"\s+", " ", title).strip()

    typ = "zapytanie" if re.match(r"Zapytanie", title, re.I) else "interpelacja"

    nr_m = re.search(r"nr\s+([0-9]+/[0-9]{4})", title, re.I)
    cri = nr_m.group(1) if nr_m else ""

    data_wplywu = normalize_date(title)
    rok_m = re.search(r"\b(20\d{2})\b", title)
    rok = int(rok_m.group(1)) if rok_m else 0

    # autor w dopełniaczu — wiele form na BIP: "Pana/Pani/Panów", "Państwa",
    # "Pana Radnego/Pani Radnej", gołe "Radnego/Radnej/Radny/Radna/Radni".
    autor = ""
    autor_re = re.compile(
        r"(?:Panów Radnych|Pana Radnego|Pani Radnej|Państwa|Panów|Pana|Pani|"
        r"Radnego Rady|Radnej Rady|Radny Rady|Radna Rady|Radni Rady|Radne Rady|"
        r"Radnego|Radnej|Radny|Radna|Radni|Radne)\s+"
        r"([A-ZĄĆĘŁŃÓŚŹŻŹ][\w.\-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻŹ][\w.\-]+)*)"
    )
    for src in (title, listing_title):
        m = autor_re.search(src)
        if m:
            autor = m.group(1).strip().split(" i ")[0].split(",")[0].strip()
            if autor:
                break
    # czasem "radnego X" po "Pan" (więc wyżej już objęte) — fallback: po "Rady Miasta"
    if not autor:
        m = re.search(
            r"(?:Rada Miasta\s+)?(?:Pan\s+)?([A-ZĄĆĘŁŃÓŚŹŻŹ][\w.\-]+\s+[A-ZĄĆĘŁŃÓŚŹŻŹ][\w.\-]+)",
            title,
        )
        if m:
            autor = m.group(1).strip()
    radny = _resolve_radny(autor)

    # przedmiot "dotyczy ..." — ze szczegółów, potem z listingu
    przed = ""
    for src in (title, listing_title):
        dot = re.search(r"dotycz[y]?\s+(.*)", src, re.I)
        if dot and dot.group(1).strip():
            przed = re.sub(r"\s+", " ", dot.group(1)).strip()
            przed = re.sub(r"\s+WIĘCEJ\s*$", "", przed)
            if przed:
                break

    files = re.findall(r'href="([^"]+\.pdf)"', h)
    tresc_url, odpowiedz_url = "", ""
    for u in files:
        full = BIP_BASE + u if u.startswith("/") else u
        low = u.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = full
        elif not tresc_url:
            tresc_url = full

    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    kadencja = _KADENCJA.get(str(rok), "2018-2024")

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przed,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": bip_url,
    }


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
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


def parse_listing(html: str) -> list[tuple[str, str]]:
    """[(detail_url, title)] z listingu."""
    if not html:
        return []
    out = []
    for m in re.finditer(r'<li[^>]*class="[^"]*ak_\d[^"]*"[^>]*>(.*?)</li>', html, re.S):
        body = m.group(1)
        href = re.search(r'href="([^"]+)"', body)
        if not href:
            continue
        u = href.group(1)
        txt = re.sub(r"<[^>]+>", " ", body)
        txt = re.sub(r"\s+", " ", txt).strip()
        if u.startswith("/"):
            u = BIP_BASE + u
        if not re.search(r"/99282-interpelacje-i-zapytania-radnych/\d+", u):
            continue
        out.append((u, txt))
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Rzeszowa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też starsze kadencje; domyślnie tylko 2024+")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="Limit stron (test)")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Rzeszowa ===")
    seen: dict[str, str] = {}
    empty_streak = 0
    page = 1
    while page <= args.max_pages:
        url = REJESTR_URL if page == 1 else f"{REJESTR_URL}?strona={page}"
        time.sleep(DELAY)
        html = fetch_text(session, url)
        items = parse_listing(html)
        new = [(u, t) for (u, t) in items if u not in seen]
        _log(f"  strona {page}: {len(items)} pozycji, nowych {len(new)}")
        if not new:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0
        for u, t in new:
            seen[u] = t
        if page % 10 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)} zebranych)")
        page += 1
    print(f"  Listing: {len(seen)} rekordów (do strony {page - 1})")

    records = []
    fetched = 0
    for i, url in enumerate(list(seen), start=1):
        time.sleep(DELAY)
        html = fetch_text(session, url)
        rec = parse_detail(html, url, seen[url])
        if not rec:
            continue
        if not rec["rok"]:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        fetched += 1
        if fetched % 50 == 0:
            print(f"  szczegóły: {fetched}...")

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
