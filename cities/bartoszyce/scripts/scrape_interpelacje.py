#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Bartoszyce.

Źródło: BIP Bartoszyc — "Interpelacje i zapytania" (Rada Miasta).

    https://bip.bartoszyce.pl/10010/Interpelacje_i_zapytania/

eSesja (https://bartoszyce.esesja.pl) NIE publikuje interpelacji — moduł
"Interpelacje i zapytania" jest pustą powłoką JS bez danych. Źródłem jest
wyłącznie rejestr na BIP.

Struktura:
  * Listing = lista informacji z paginacją `.../Interpelacje_i_zapytania/2/`
    (oraz `.../1/archiwum/Interpelacje_i_zapytania/` dla starszych wpisów).
    Każdy rekord to link do strony szczegółów: /10010/{id}/{slug}/
  * Szczegóły = strona BIP z tytułem w `p.phx.ph3` o formacie:
        {Typ} radnego/radnej {Imię Nazwisko} z dnia {DD miesiąc RRRR} r.
        {temat}[/ oraz odpowiedź ...]
    oraz załącznikami (ul.attachments → li → a): PDF treści i/lub PDF
    odpowiedzi ("Odpowiedź...").

Output: recordy w formacie Radoskop (ten sam schemat co Przemyśl/Warszawa):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # też starsze niż 2024
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

BASE_URL = "https://bip.bartoszyce.pl"
LISTING_URL = f"{BASE_URL}/10010/Interpelacje_i_zapytania/"
ARCHIVE_URL = f"{BASE_URL}/10010/1/archiwum/Interpelacje_i_zapytania/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
MAX_PAGES = 60
MIN_ROK_DEFAULT = 2024  # bieżąca (IX) kadencja 2024-2029

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


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DETAIL_RE = re.compile(r'href="(https://bip\.bartoszyce\.pl/10010/(\d+)/[^"]+/)"')


def parse_listing(html: str) -> list[tuple[str, str]]:
    """Zwraca [(url, id)] rekordów interpelacji z listingu (dedupe po URL)."""
    out: dict[str, str] = {}
    if html:
        for m in _DETAIL_RE.finditer(html):
            url, cid = m.group(1), m.group(2)
            # pomiń fałszywe dopasowania (paginacja / archiwum mają id 1)
            if cid == "1":
                continue
            out[url] = cid
    return list(out.items())


_MIESIACE = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
}

# Oddzielny symbol-słowny przetwarzacz tytułu. Tytuły występują w kilku
# formatach, np.:
#   Zapytanie radnego Karola Kapuścińskiego z dnia 22 lipca 2026 r. dotyczące ...
#   Zapytanie radnego Karola Kapuścińskiego z 24 kwietnia 2026 r. ...
#   Zapytanie radnego X z dnia 12_01_2026 r. ...          (data mm_yyyy)
#   2024_07_18 - Zapytanie radnego Mateusza Golombiowskiego dotyczące ...  (prefiks daty)
# Radny w tytule jest w dopełniaczu ("Karola Kapuścińskiego"); normalizujemy
# go do mianownika względem listy radnych z config.json (club_assignments).

_WORD_CAP = r"[A-ZŻŹĆĄŚĘŁÓŃ][a-ząęółśżźćń]+"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

# "z dnia 22 lipca 2026 r." lub "z 24 kwietnia 2026 r."
_DATE_PL = re.compile(
    r"\s*z\s+(?:dnia\s+)?(?P<dz>\d{1,2})\s+(?P<mc>[a-ząęółśżźćń]+)\s+(?P<rok>\d{4})\s*r\.?",
    re.I,
)
# "z dnia 12_01_2026" / "12.01.2026" / "12-01-2026" numericzne
_DATE_NUM = re.compile(r"\s*z\s+(?:dnia\s+)?(?P<dz>\d{1,2})[\s_\-\.,/]+(?P<mc>\d{1,2})[\s_\-\.,/]+(?P<rok>\d{4})")
# prefiks daty na początku tytułu: "2024_07_18 - "
_DATE_PREFIX = re.compile(r"^\s*(?P<rok>\d{4})[\s_\-\.,/](?P<mc>\d{1,2})[\s_\-\.,/](?P<dz>\d{1,2})\s*[-–—]?\s*")
# marker radny: radnego/radnej {Imię Nazwisko...}
_RADNY_RE = re.compile(r"\bradne(?:go|j)\s+(?P<name>(?:_WORD_CAP_\s*)+)".replace("_WORD_CAP_", _WORD_CAP))


def _norm_date(dz, mc, rok):
    try:
        mies = mc.strip().lower()
        if mies in _MIESIACE:
            m = _MIESIACE[mies]
        else:
            m = str(int(mies)).zfill(2)
        return f"{int(rok):04d}-{m}-{int(dz):02d}"
    except Exception:
        return ""


def _normalize_radny(captured: str) -> str:
    """Dopełniacz do mianownika — najlepsze dopasowanie do listy radnych
    z config.json (club_assignments) na podstawie podobieństwa tokenów."""
    if not captured:
        return ""
    from difflib import SequenceMatcher
    cap = [t for t in captured.split()]
    best, bestscore = "", 0.0
    for cname in _CLUB_ASSIGN:
        c = [t for t in cname.split()]
        if not c or not cap:
            continue
        # nazwisko (ostatni token obu) musi być podobne
        if SequenceMatcher(None, c[-1].lower(), cap[-1].lower()).ratio() < 0.6:
            continue
        # podobieństwo imienia (pierwsze tokeny)
        if len(c) >= 2 and len(cap) >= 2:
            if SequenceMatcher(None, c[0].lower(), cap[0].lower()).ratio() < 0.5:
                continue
        score = sum(
            max(SequenceMatcher(None, ct.lower(), pt.lower()).ratio() for pt in cap)
            for ct in c
        ) / len(c)
        if score > bestscore:
            bestscore, best = score, cname
    if best and bestscore >= 0.6:
        return best
    return captured


def parse_title(title: str):
    """Rozbija tytuł na typ/radny/data_wplywu/przedmiot."""
    t = _clean(title)
    if not t:
        return None

    data_wplywu = ""
    rok = 0

    # 1) prefiks daty na początku tytułu
    m = _DATE_PREFIX.match(t)
    if m:
        data_wplywu = _norm_date(m.group("dz"), m.group("mc"), m.group("rok"))
        t = t[m.end():]
        rok = int(m.group("rok"))

    # 2) radny (dopełniacz)
    m = _RADNY_RE.search(t)
    if m:
        captured = _clean(m.group("name"))
        radny_nom = _normalize_radny(captured)
        # typ = pierwszy token przed "radnego/radnej"
        pre = t[:m.start()]
        toks = [x for x in re.split(r"[\s\-–—]+", pre) if x]
        typ_raw = (toks[0] if toks else "").lower()
    else:
        captured = ""
        radny_nom = ""
        typ_raw = (t.split()[0] if t.split() else "").lower()
        typ_raw = typ_raw.strip("-_–—")

    # 3) data "z dnia DD miesiąc RRRR" / "z DD miesiąc RRRR" / numeryczna
    if not data_wplywu:
        m2 = _DATE_PL.search(t)
        if m2:
            data_wplywu = _norm_date(m2.group("dz"), m2.group("mc"), m2.group("rok"))
            rok = int(m2.group("rok"))
        else:
            m3 = _DATE_NUM.search(t)
            if m3:
                data_wplywu = _norm_date(m3.group("dz"), m3.group("mc"), m3.group("rok"))
                rok = int(m3.group("rok"))
    if not rok and data_wplywu:
        rok = int(data_wplywu[:4])

    # 4) przedmiot: usuń klauzule daty + "oraz odpowiedź..."
    if m:
        t2 = t[m.end():]
    else:
        t2 = t
    t2 = _DATE_PL.sub(" ", t2)
    t2 = _DATE_NUM.sub(" ", t2)
    przedmiot = re.split(r"\s+oraz\s+odpowiedzi?a", t2, flags=re.I)[0].strip()
    przedmiot = przedmiot.strip(" -–—") or typ_raw

    # typ znormalizowany do słownika Radoskop
    if "interpelacj" in typ_raw:
        typ = "interpelacja"
    elif "zapytan" in typ_raw or "prośb" in typ_raw or "prosb" in typ_raw or "pytan" in typ_raw:
        typ = "zapytanie"
    elif "wniosek" in typ_raw:
        typ = "wniosek"
    else:
        typ = typ_raw or "interpelacja"

    return {
        "typ": typ,
        "radny": radny_nom or captured,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "przedmiot": przedmiot,
    }


_ATT_RE = re.compile(r'<li[^>]*>\s*<a href="([^"]+)"[^>]*>.*?<span>(.*?)</span></a>', re.S)
_DATA_WYTW_RE = re.compile(r"Data wytworzenia informacji:\s*<em>(\d{4}-\d{2}-\d{2})</em>")


def parse_detail(html: str, bip_url: str, cid: str) -> dict | None:
    if not html:
        return None
    # tytuł
    m = re.search(r'<p class="phx ph3">(.*?)</p>', html, re.S)
    title = _clean(m.group(1)) if m else ""
    if not title:
        return None
    info = parse_title(title)
    if not info:
        return None

    # załączniki: (label, url) — treść i odpowiedź
    atts = []
    for url, label in _ATT_RE.findall(html):
        url = url.replace("&amp;", "&")
        atts.append((_clean(label), url))

    tresc_url, odpowiedz_url = "", ""
    for label, url in atts:
        low = label.lower()
        if low.startswith("odpowied"):
            if not odpowiedz_url:
                odpowiedz_url = url
        elif not tresc_url:
            tresc_url = url
    if not tresc_url and atts:
        tresc_url = atts[0][1]

    # data odpowiedzi: z "Data wytworzenia informacji" przy pliku odpowiedzi
    data_odpowiedzi = ""
    if odpowiedz_url:
        seg = html[html.find(odpowiedz_url): html.find(odpowiedz_url) + 1200]
        dm = _DATA_WYTW_RE.search(seg)
        if dm:
            data_odpowiedzi = dm.group(1)

    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    return {
        "cri": cid,
        "typ": info["typ"],
        "rok": info["rok"],
        "kadencja": "2024-2029" if info["rok"] >= 2024 else "2018-2024",
        "radny": info["radny"],
        "przedmiot": info["przedmiot"],
        "data_wplywu": info["data_wplywu"],
        "klub": _club_for_radny(info["radny"]),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": bip_url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Bartoszyc"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też wpisy sprzed 2024; domyślnie tylko bieżąca kadencja",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Bartoszyc ===")
    seen: dict[str, str] = {}  # url -> cid

    def _walk(base: str, label: str):
        page = 1
        empty_streak = 0
        while page <= MAX_PAGES:
            url = base if page == 1 else f"{base}{page}/"
            html = fetch_text(session, url)
            items = parse_listing(html)
            new = [(u, c) for u, c in items if u not in seen]
            _log(f"  {label} strona {page}: {len(items)} linków, nowych {len(new)}")
            if not new:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
            for u, c in new:
                seen[u] = c
            if page % 5 == 0 and not _DEBUG:
                print(f"  {label} strona {page}... (łącznie {len(seen)})")
            page += 1
        print(f"  {label}: {len(seen)} do tej pory (do strony {page - 1})")

    _walk(LISTING_URL, "Rejestr")
    # archiwum (starsze wpisy)
    _walk(ARCHIVE_URL, "Archiwum")

    records = []
    fetched = 0
    for i, (url, cid) in enumerate(seen.items(), start=1):
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url, cid)
        if not rec:
            print(f"  [skip] nie sparsowano: {url}")
            continue
        fetched += 1
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if fetched % 20 == 0:
            print(f"  szczegóły: {fetched}...")

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Więcej typów:  {len(records) - interp - zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
