#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Częstochowy.

Źródło: BIP Częstochowy — moduł "Interpelacje i zapytania Radnych"
(https://bip.czestochowa.pl/interpelacje/szukaj). To jest ten sam silnik CCT
co w Przemyślu, ale z inną strukturą HTML (listing = tabela table-borderless
z captionami "Interpelacja w sprawie"/"Zapytanie w sprawie" + wiersz
"Tożsamość radnego"; szczegóły = atrybuty Typ wystąpienia / Tożsamość radnego
/ w sprawie oraz sekcja Załączniki z PDF-ami "Interpelacja" i "Odpowiedź").

Kadencje (term_id):
  * term_id=4  -> IX kadencja (2024-2029)      [domyślna]
  * term_id=3  -> VIII kadencja (2018-2024)
  * term_id=2  -> VII kadencja (2014-2018)

Paginacja rejestru: ?term_id=N&page=P&perPage=R (max 122 stron / 10-na-stronę
dla IX kadencji).

Output: lista rekordów w formacie Radoskop (ten sam schemat co Warszawa/
Bydgoszcz/Przemyśl):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego jest brany z config.json (club_assignments -> clubs), tak samo
jak w scrape_czestochowa.py. Dane bieżące (wystąpienia radnych IX kadencji)
jednoznacznie mapują się do radnych z configa.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all  # także VIII i VII
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

from http_cache import init_cache, cached_fetch_text  # noqa: E402

# TTL cache strony szczegółowe — stabilne URL-e, treść zmienia się dopiero
# gdy pojawi się odpowiedź. Listingi zawsze force (stronicowanie się przesuwa).
DETAIL_TTL = 3 * 86400

# Rejestr interpelacji/zapytań — moduł CCT na BIP Częstochowy.
LISTING_URL = "https://bip.czestochowa.pl/interpelacje/szukaj"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
MAX_PAGES = 400
PER_PAGE = 50  # serwer przyjmuje (zweryfikowane live); dawniej 10 → 400 stron listy
# Domyślnie tylko bieżąca kadencja (IX, 2024-2029). Starsze — przez --all.
MIN_ROK_DEFAULT = 2024

# term_id -> kadencja
TERMS = {
    4: "2024-2029",
    3: "2018-2024",
    2: "2014-2018",
}

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


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


def _canonical_radny(radny: str) -> str:
    """Przywraca kanoniczną formę 'Imię Nazwisko' radnego z BIP.

    BIP Częstochowy potrafi dopisać za myślnikiem rolę ("- radny", "- radna",
    "- Przewodniczący Rady Miasta Częstochowy"). Mapujemy z powrotem do nazwy,
    pod którą radny figuruje w config (club_assignments), o ile da się jedno-
    znacznie dopasować prefix.
    """
    if not radny:
        return radny
    # dokładne dopasowanie
    if radny in _CLUB_ASSIGN:
        return radny
    # prefix: "Imię Nazwisko" na początku, reszta za " - " to rola
    base = radny.split(" - ")[0].strip()
    # dopasuj najdłuższy prefiks wśród znanych radnych (np. dwuczłonowe nazwiska)
    best = ""
    for name in _CLUB_ASSIGN:
        if radny.startswith(name) or radny.replace("- ", " ").startswith(name):
            if len(name) > len(best):
                best = name
    if best:
        return best
    return base


def _club_for_radny(radny: str) -> str:
    radny = _canonical_radny(radny)
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    name = club.get("name", "") if isinstance(club, dict) else ""
    return name if name else code


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session: requests.Session, url: str, *, force: bool = False,
               ttl: float | None = DETAIL_TTL) -> str:
    """Fetch z disk cache (TTL) + politeness delay + retry na 403/5xx."""
    for attempt in range(3):
        try:
            return cached_fetch_text(url, session=session, timeout=30,
                                     delay=0.2, force=force, ttl=ttl)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def fetch_listing(session: requests.Session, term_id: int, page: int) -> str:
    url = f"{LISTING_URL}?keyword=&type_id=-1&number=&regarding=&councillor=&term_id={term_id}&page={page}&perPage={PER_PAGE}"
    time.sleep(DELAY)
    # Listing: zawsze HTTP — nowe wpisy przesuwa stronicowanie.
    return fetch_text(session, url, force=True, ttl=0)


# ---------------------------------------------------------------------------
# Parsing — listing
# ---------------------------------------------------------------------------

def parse_listing(html: str) -> list[dict]:
    """Extract {url, typ, przedmiot, radny} per record from the listing.

    Structura: każdy rekord to <div><table class="table table-borderless">
    z captionem ("Interpelacja w sprawie : X" | "Zapytanie w sprawie : X")
    oraz wierszami: "Interpelacja/Zapytanie w sprawie" (z linkiem) oraz
    "Tożsamość radnego" (np. "Piotr Wrona - radny").
    """
    if not html:
        return []
    out = []
    # split per record block
    for block in re.findall(
        r'<div\s*>\s*<table class="table table-borderless">(.*?)</table>', html, re.S
    ):
        caption = ""
        m = re.search(r"<caption[^>]*>(.*?)</caption>", block, re.S)
        if m:
            caption = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        # typ i przedmiot z captionu
        cm = re.match(r"^(Interpelacja|Zapytanie)\s*w sprawie\s*[:\-]?\s*(.*)$", caption, re.S)
        if not cm:
            # fallback: z pierwszego th
            thm = re.search(r'<th scope="row">(Interpelacja|Zapytanie) w sprawie</th>', block)
            typ = thm.group(1).lower() if thm else "interpelacja"
            przedmiot = caption
        else:
            typ = cm.group(1).lower()
            przedmiot = cm.group(2).strip()
        # link do szczegółów
        link_m = re.search(r'href="(https://bip\.czestochowa\.pl/interpelacja/[^"]+)"', block)
        url = link_m.group(1).strip() if link_m else ""
        # radny
        rad_m = re.search(
            r'<th scope="row">Tożsamość radnego</th>\s*<td[^>]*>(.*?)</td>', block, re.S
        )
        radny_raw = re.sub(r"<[^>]+>", " ", rad_m.group(1)).strip() if rad_m else ""
        radny = re.sub(r"\s*-\s*(radny|radna)\s*$", "", radny_raw).strip()
        if url:
            out.append({
                "url": url,
                "typ": typ,
                "przedmiot": przedmiot,
                "radny": radny,
            })
    return out


# ---------------------------------------------------------------------------
# Parsing — szczegóły
# ---------------------------------------------------------------------------

def parse_detail(html: str, bip_url: str) -> dict | None:
    if not html:
        return None

    typ = "interpelacja"
    m = re.search(r'<th[^>]*>\s*Typ wystąpienia\s*</th>\s*<td[^>]*>(.*?)</td>', html, re.S)
    if m:
        raw = re.sub(r"<[^>]+>", " ", m.group(1)).strip().lower()
        typ = "zapytanie" if "zapytanie" in raw else "interpelacja"

    radny = ""
    m = re.search(
        r'<th[^>]*>\s*Tożsamość radnego\s*</th>\s*<td[^>]*>(.*?)</td>', html, re.S
    )
    if m:
        radny = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        radny = _canonical_radny(radny)

    przedmiot = ""
    m = re.search(r'<th[^>]*>\s*w sprawie\s*</th>\s*<td[^>]*>(.*?)</td>', html, re.S)
    if m:
        przedmiot = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()

    # Załączniki: sekcja id="attachments" — a href="/attachments/download/NNN"
    # z etykietą tekstową ("Interpelacja" lub "Odpowiedź").
    tresc_url = ""
    odpowiedz_url = ""
    att = ""
    m = re.search(r'<section id="attachments".*?</section>', html, re.S)
    if m:
        att = m.group(0)
    for href, label in re.findall(
        r'<a[^>]*href="(https://bip\.czestochowa\.pl/attachments/download/\d+)"[^>]*>(.*?)</a>', att, re.S
    ):
        lbl = re.sub(r"<[^>]+>", " ", label).strip().lower()
        if "odpowied" in lbl:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif not tresc_url:
            tresc_url = href

    # data_wplywu = Data wytworzenia w metryczce załącznika treści (z <time datetime="YYYY-MM-DD">)
    data_wplywu = _attachment_date(html, "Interpelacja")
    data_odpowiedzi = _attachment_date(html, "Odpowiedź")

    year = int(data_wplywu[:4]) if len(data_wplywu) >= 4 and data_wplywu[:4].isdigit() else 0
    kadencja = _kadencja_for_year(year)

    return {
        "cri": _cri_from_url(bip_url),
        "typ": typ,
        "rok": year,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if data_odpowiedzi else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": bip_url,
    }


def _attachment_date(html: str, label: str) -> str:
    """Data wytworzenia (<time datetime="YYYY-MM-DD">) załącznika o danej etykiecie."""
    # Find the attachment header block for this label, then the first
    # <time datetime> within its metryczka.
    for m in re.finditer(r'<div class="header">(.*?)</div>', html, re.S):
        block = m.group(1)
        if re.search(r">\s*" + re.escape(label) + r"\s*<", block):
            # metryczka comes right after this header div
            rest = html[m.end():]
            tm = re.search(r'<time\s+datetime="(\d{4}-\d{2}-\d{2})"', rest)
            if tm:
                return tm.group(1)
            break
    return ""


def _cri_from_url(url: str) -> str:
    m = re.search(r"/interpelacja/(\d+)/", url)
    return m.group(1) if m else ""


def _kadencja_for_year(year: int) -> str:
    if year >= 2024:
        return "2024-2029"
    if year >= 2018:
        return "2018-2024"
    if year >= 2014:
        return "2014-2018"
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Częstochowy"
    )
    parser.add_argument("--output", default="docs/interpelacje.json", help="Plik wyjściowy")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalnie)")
    parser.add_argument("--debug", action="store_true", help="Szczegółowe logowanie")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też starsze kadencje (VIII 2018-2024, VII 2014-2018); "
             "domyślnie tylko IX (2024-2029)",
    )
    args = parser.parse_args()

    _DEBUG = args.debug
    terms = sorted(TERMS.keys()) if args.all else [4]

    init_cache(args.cache_dir)

    session = _session()

    print("=== Interpelacje i zapytania — BIP Częstochowy ===")
    all_listed: list[dict] = []
    for term_id in terms:
        kad_label = TERMS[term_id]
        print(f"--- kadencja {kad_label} (term_id={term_id}) ---")
        seen: dict[str, dict] = {}
        empty_streak = 0
        page = 1
        while page <= MAX_PAGES:
            html = fetch_listing(session, term_id, page)
            recs = parse_listing(html)
            new = [r for r in recs if r["url"] not in seen]
            _log(f"  strona {page}: {len(recs)} rekordów, nowych: {len(new)}")
            if not recs:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
            for r in new:
                seen[r["url"]] = r
            if page % 10 == 0 and not _DEBUG:
                print(f"  listing strona {page}... ({len(seen)} znalezionych)")
            page += 1
            if page > 1 and not new and not recs:
                break
        print(f"  Listing: {len(seen)} wystąpień w kadencji {kad_label}")
        all_listed.extend(seen.values())

    print(f"Razem w liście: {len(all_listed)} wystąpień")

    records = []
    fetched = 0
    for i, rec in enumerate(all_listed, start=1):
        html = fetch_text(session, rec["url"])
        if not html:
            print(f"  [skip] brak treści: {rec['url']}")
            continue
        detail = parse_detail(html, rec["url"])
        if not detail:
            continue
        fetched += 1
        # fallback: pola z listingu gdy szczegóły niekompletne
        if not detail["przedmiot"]:
            detail["przedmiot"] = rec["przedmiot"]
        if not detail["radny"]:
            detail["radny"] = rec["radny"]
        records.append(detail)
        if fetched % 100 == 0:
            print(f"  szczegóły: {fetched}...")

    # Filtr kadencji: domyślnie tylko bieżąca (rok >= 2024).
    if not args.all:
        records = [r for r in records if r["rok"] >= MIN_ROK_DEFAULT or not r["rok"]]

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
