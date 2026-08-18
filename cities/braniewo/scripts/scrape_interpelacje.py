#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Braniewie.

Źródło: BIP Braniewa — rejestr "Interpelacje i zapytania radnych".

    http://bip.braniewo.pl/interpelacje/38

eSesja (https://braniewo.esesja.pl) to tu NIEPRAWIDŁOWA rada (powiat
braniewski) — Rada Miasta Braniewa publikuje interpelacje wyłącznie w
rejestrze na BIP (CMS Logonet).

Struktura:
  * Listing = lista tabel (`table.table-borderless`), po 25 na stronę, z
    paginacją `/interpelacje/{page}/{perPage}`. Każdy rekord:
        Interpelacja w sprawie / Zapytanie w sprawie -> link do szczegółów
        Nr sprawy           -> np. OR.0003.8.2026.MK
        Tożsamość radnego   -> "Radny Kamil Rant" / "Radna Joanna ..."
  * Szczegóły = strona `/interpelacja/{id}/slug`:
        tabela "Szczegóły" (Typ wystąpienia, Nr sprawy, Tożsamość radnego,
        w sprawie) + sekcja "Załączniki" — każdy załącznik
        (`/attachments/download/{id}`) z metryczką (Wytworzył, Data
        wytworzenia). Załącznik wytworzony przez radnego = treść interpelacji;
        przez Burmistrza/Zastępcę = odpowiedź.

Output: rekordy w formacie Radoskop (ten sam schemat co Przemyśl/Bartoszyce):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także starsze kadencje
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

BASE_URL = "http://bip.braniewo.pl"
LISTING_URL = f"{BASE_URL}/interpelacje/38"
PER_PAGE = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
MAX_PAGES = 80
# Domyślnie tylko bieżąca kadencja (IX, 2024-2029).
MIN_ROK_DEFAULT = 2024

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


from difflib import SequenceMatcher

_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?:radny\s+radn[ae]|radn[ao]|radni:?|radni\s+|przewodnicz[ąa]cy\s+rady\s+miejskiej|"
    r"wiceprzewodnicz[ąa]cy\s+rady\s+miejskiej|wiceprzewodnicz[ąa]ca\s+rady\s+miejskiej|"
    r"przewodnicz[ąa]cy\s+rady\s+miasta|rady\s+miejskiej|rady\s+miasta)\s+",
    re.I,
)


def _clean_radny(raw: str) -> str:
    """Normalizuje "Tożsamość radnego" do mianownika (gdy możliwe) i do zbioru
    nazw z config club_assignments — dla spójności nazw i klubu.

    Obsługuje role (Radny/Radna/Wiceprzewodniczący...), wielu autorów
    ("Radny X, Radny Y") oraz skróty z inicjałami ("P.Gnatek").
    """
    s = raw.strip()
    # usuń prefiksy ról/rady
    s = _ROLE_PREFIX_RE.sub(" ", s).strip()
    # pierwszy autor (przed przecinkiem / i / oraz)
    s = re.split(r"[,;&]|\s+oraz\s+", s)[0].strip()
    if not s:
        return ""

    # dokładne dopasowanie
    if s in _CLUB_ASSIGN:
        return s

    # dopasowanie po nazwisku (ostatni token) do listy radnych z config
    s_tokens = s.split()
    surname = s_tokens[-1].lower().rstrip(".")
    best, bestscore = "", 0.0
    for cname in _CLUB_ASSIGN:
        ct = cname.split()
        if not ct:
            continue
        if SequenceMatcher(None, ct[-1].lower(), surname).ratio() < 0.6:
            continue
        score = SequenceMatcher(None, ct[-1].lower(), surname).ratio()
        if score > bestscore:
            bestscore, best = score, cname
    if best and bestscore >= 0.6:
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
            resp = session.get(url, timeout=30, verify=False)
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
# Parsing listingu
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r'<table class="table table-borderless">(.*?)</table>', re.S)


def _collapse(html: str) -> str:
    """Normalizuje białe znaki między tagami (Logonet czasem wstawia
    nowe linie między `</th>` a `<td>` — czyli „pretty print")."""
    return re.sub(r">\s+<", "><", html)


def _td(html: str, label: str) -> str:
    """Wartość z wiersza <th scope='row'>{label}</th><td...>{value}</td>."""
    m = re.search(
        re.escape(label) + r"</th><td[^>]*>(.*?)</td>", html, re.S
    )
    if not m:
        return ""
    v = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", v).strip()


def parse_listing(html: str):
    """Zwraca listę rekordów {typ, temat, nr, radny, detail_url}."""
    out = []
    if not html:
        return out
    for tb in _TABLE_RE.findall(html):
        tb = _collapse(tb)
        # typ z pierwszego <th scope="row">
        m = re.search(r'<th scope="row">([^<]+)</th>', tb)
        label = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        d = re.search(r'href="(http://bip\.braniewo\.pl/interpelacja/[^"]+)"', tb)
        detail_url = d.group(1) if d else ""
        nr = _td(tb, "Nr sprawy")
        radny_raw = _td(tb, "Tożsamość radnego")
        if not detail_url and not nr:
            continue
        temat = re.sub(r"\s+", " ", label).strip()
        # typ
        low = label.lower()
        if "zapytan" in low:
            typ = "zapytanie"
        elif "wniosek" in low or "wniosk" in low:
            typ = "wniosek"
        else:
            typ = "interpelacja"
        out.append(
            {
                "typ": typ,
                "temat": temat,
                "nr": nr,
                "radny_raw": radny_raw,
                "detail_url": detail_url,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Parsing szczegółów
# ---------------------------------------------------------------------------

_ATT_HDR_RE = re.compile(
    r'<div class="header">.*?attachments/download/(\d+)"[^>]*>\s*(.*?)\s*</a>',
    re.S,
)
_LEGAL_RE = re.compile(
    r'<div class="legal[^"]*"[^>]*>.*?</div>\s*</div>', re.S
)


def _creator_and_date(legal_html: str) -> tuple[str, str]:
    c = re.search(r"<th>Wytworzył:</th><td>(.*?)</td>", legal_html, re.S)
    creator = re.sub(r"\s+", " ", c.group(1)).strip() if c else ""
    # prefer <time datetime="YYYY-MM-DD">; fallback do widocznej daty DD.MM.YYYY
    d = re.search(r'<time datetime="([\d-]+)"', legal_html)
    date = ""
    if d:
        date = d.group(1)
    else:
        m = re.search(
            r"<th>Data wytworzenia:</th><td>(?:<[^>]+>)?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})",
            legal_html,
        )
        if m:
            dd, mm, yy = m.groups()
            date = f"{yy}-{int(mm):02d}-{int(dd):02d}"
    return creator, date


def parse_detail(html: str, bip_url: str, listing_nr: str, listing_typ: str) -> dict | None:
    if not html:
        return None
    html = _collapse(html)

    # tabela "Szczegóły"
    detail_tbl = _TABLE_RE.search(html)
    if detail_tbl:
        dhtml = detail_tbl.group(1)
    else:
        dhtml = html

    typ_txt = _td(dhtml, "Typ wystąpienia")
    nr = _td(dhtml, "Nr sprawy") or listing_nr
    radny_raw = _td(dhtml, "Tożsamość radnego") or ""
    temat = _td(dhtml, "w sprawie")

    if not typ_txt and listing_typ:
        typ_txt = listing_typ
    low = typ_txt.lower()
    if "zapytan" in low:
        typ = "zapytanie"
    elif "wniosek" in low or "wniosk" in low:
        typ = "wniosek"
    else:
        typ = "interpelacja"

    # radny w mianowniku (usuwa role, wielu autorów, inicjały -> nazwa z config)
    radny = _clean_radny(radny_raw)

    # załączniki: nagłówki (id,label) + metryczki (creator,date) w kolejności
    atts = []
    hdrs = [(int(i), re.sub(r"\s+", " ", lab).strip())
            for i, lab in _ATT_HDR_RE.findall(html)]
    legals = re.findall(
        r'<div class="legal file_legal_\d+".*?</time>.*?data-wytworzenia.*?</tr>.*?</tr>',
        html,
        re.S,
    )
    if not legals:
        # prostsza metryczka — bez <time>
        legals = re.findall(
            r'<div class="legal file_legal_\d+"[^>]*>(.*?)(?=<div class="header"|</section>)',
            html,
            re.S,
        )
    for i, (aid, label) in enumerate(hdrs):
        creator, date = "", ""
        if i < len(legals):
            creator, date = _creator_and_date(legals[i])
        atts.append(
            {
                "id": aid,
                "url": f"{BASE_URL}/attachments/download/{aid}",
                "label": label,
                "creator": creator,
                "date": date,
            }
        )

    # treść = załącznik wytworzony przez radnego; odpowiedź = inny (Burmistrz/Zastępca)
    radny_surname = radny.split()[-1].lower() if radny.split() else ""

    def _radny_made(a):
        c = a["creator"].lower()
        if "radn" in c:
            return True
        if radny_surname and radny_surname in c:
            return True
        # label z interpelacja/zapytanie i brak responsa
        return False

    tresc_cands = [a for a in atts if _radny_made(a)]
    odp_cands = [a for a in atts if a not in tresc_cands]

    tresc = tresc_cands[0] if tresc_cands else (atts[0] if atts else None)
    odp = odp_cands[0] if odp_cands else None

    tresc_url = tresc["url"] if tresc else ""
    odpowiedz_url = odp["url"] if odp else ""
    data_wplywu = (tresc or odp or {}).get("date", "")
    if tresc and tresc.get("date"):
        data_wplywu = tresc["date"]
    data_odpowiedzi = odp["date"] if odp else ""
    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    rok = 0
    mrok = re.search(r"(20\d{2})", nr or "")
    if mrok:
        rok = int(mrok.group(1))
    if not rok and data_wplywu:
        rok = int(data_wplywu[:4])

    return {
        "cri": nr,
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2023",
        "radny": radny,
        "przedmiot": temat,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
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
        description="Scraper interpelacji i zapytań radnych z BIP Braniewa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też starsze kadencje (sprzed 2024); domyślnie bieżąca",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Ograniczenie liczby szczegółów do scrapowania (do testów)",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Braniewa ===")
    listings: list[dict] = []
    seen_nr: set[str] = set()
    page = 1
    empty_streak = 0
    while page <= MAX_PAGES:
        url = f"{BASE_URL}/interpelacje/{page}/{PER_PAGE}"
        html = fetch_text(session, url)
        items = parse_listing(html)
        new = [it for it in items if it["nr"] not in seen_nr]
        for it in new:
            seen_nr.add(it["nr"])
        _log(f"  strona {page}: {len(items)} rekordów, nowych {len(new)}")
        if not new:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        listings.extend(new)
        if page % 5 == 0 and not _DEBUG:
            print(f"  listing strona {page}... (łącznie {len(listings)})")
        page += 1
    print(f"  Listing: {len(listings)} rekordów w rejestrze (do strony {page - 1})")

    records = []
    fetched = 0
    for it in listings:
        if args.limit and len(records) >= args.limit:
            # (limit dotyczy rekordów po filtrze roku — do testów)
            break
        url = it["detail_url"]
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url, it["nr"], it["typ"])
        if not rec:
            continue
        fetched += 1
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if fetched % 25 == 0:
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
