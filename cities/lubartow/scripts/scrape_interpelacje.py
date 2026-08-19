#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Lubartów.

Źródło: BIP Lubartów (platforma "Wrota Lubelszczyzny" — bip.lubelskie.pl),
kategoria "Interpelacje i zapytania radnych" (id_menu=302):
    https://umlubartow.bip.lubelskie.pl/index.php?id=302

  * Listing = serwerowy DataTable pod `?id=302&action=list-ajax` (JSON).
  * Szczegóły = strona `?id=302&p1=szczegoly&p2={id_dokumentu}` z załącznikami
    PDF ("Plik źródłowy" = treść interpelacji/zapytania, "Odpowiedź").
  * Radny, typ oraz data wpływu NIE są w metryce BIP — są w treści PDF
    (nagłówek pisma: "<Imię Nazwisko> / Radny Rady Miasta", "Wpłynęło dn. ..",
    "INTERPELACJA"/"Zapytanie"). Parsujemy warstwę tekstową PDF (pypdf).

Output: lista rekordów w formacie Radoskop (jak Warszawa/Bydgoszcz/Przemyśl):
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Klub radnego jest brany z config.json (club_assignments -> clubs).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all  # także starsze
"""

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

REJESTR_AJAX = "https://umlubartow.bip.lubelskie.pl/index.php?id=302&action=list-ajax"
DETAIL = "https://umlubartow.bip.lubelskie.pl/index.php?id=302&p1=szczegoly&p2={pid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

DELAY = 0.5
DAO = 30  # liczba rekordów na stronę ajax (serverSide pageLength)
MIN_ROK_DEFAULT = 2024

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
    import difflib
    if not radny:
        return ""
    best = None
    best_score = 0.0
    rev = " ".join(reversed(radny.split()))
    for name in _CLUB_ASSIGN:
        score = max(
            difflib.SequenceMatcher(None, radny.lower(), name.lower()).ratio(),
            difflib.SequenceMatcher(None, rev.lower(), name.lower()).ratio(),
        )
        if score > best_score:
            best_score, best = score, name
    if best and best_score >= 0.72:
        code = _CLUB_ASSIGN.get(best, "")
        club = _CLUBS.get(code)
        if isinstance(club, dict):
            return club.get("name", "")
        return ""
    return ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_json(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return None


def fetch_html(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def fetch_bin(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=45)
            if resp.status_code == 200:
                return resp.content
            time.sleep(2)
        except requests.RequestException as e:
            _log(f"  download błąd {url}: {e}")
            time.sleep(2)
    return b""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Radny + typ + data wpływu z nagłówka pisma PDF.
_NAGLOWEK_NAME_RE = re.compile(
    r"^\s*([A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż-]+){1,4})\s*$"
)
_WPLYNELO_RE = re.compile(r"Wp[łl]yn[ęe][łl]?o\s+dn\.?\s*(\d{1,2})-(\d{1,2})-(\d{4})", re.I)
_ODP_DATA_RE = re.compile(
    r"(?:Lubart[oó]w,?\s+)?dnia\s+(\d{1,2})\s+"
    r"(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|"
    r"października|pazdziernika|listopada|grudnia)\s+(\d{4})",
    re.I,
)
_MIESIACE = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "wrze?nia": "09", "pazdziernika": "10", "października": "10",
    "listopada": "11", "grudnia": "12",
}


def _clean_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return ""
    try:
        r = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for p in r.pages[:3]:
            parts.append(p.extract_text() or "")
        return "\n".join(parts)
    except Exception as e:
        _log(f"  pdf parse error: {e}")
        return ""


def _extract_meta(text: str) -> dict:
    """Zwraca {radny, typ, data_wplywu, data_odpowiedzi} z tekstu PDF."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    radny = ""
    # Szukamy w całym tekście linii "Imię Nazwisko ... Radn(y/a/i) ..." albo
    # pary linii [Imię Nazwisko] + [Radny/Radna/Radni Rady Miasta...].
    for i, l in enumerate(lines):
        if re.search(r"\bRadn(?:y|a|i)\b", l):
            # nazwisko może być w tej samej linii przed "Radny/Radna/Radni"
            # albo w poprzedniej linii
            cand = ""
            seg = re.split(r"\s*Radn(?:y|a|i)\b", l, maxsplit=1, flags=re.I)[0]
            seg = re.sub(r"[|·•]", " ", seg).strip()
            if re.match(r"^[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż-]*){0,2}$", seg):
                cand = seg
            if not cand and i >= 1:
                prev = re.sub(r"[|·•]", " ", lines[i - 1]).strip()
                if re.match(r"^[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż-]*){1,2}$", prev):
                    cand = prev
            if cand:
                radny = cand
            break

    typ = "interpelacja"
    low_all = text.lower()
    # Zapytanie ma zwykle w nagłówku "Zapytanie Radnego"/"Zapytanie".
    if re.search(r"\bzapytanie\b", low_all) and "interpelacj" not in low_all:
        typ = "zapytanie"
    elif "interpelacja" in low_all:
        typ = "interpelacja"

    data_wplywu = ""
    m = _WPLYNELO_RE.search(text)
    if m:
        d, mo, y = m.groups()
        data_wplywu = f"{y}-{int(mo):02d}-{int(d):02d}"

    data_odpowiedzi = ""
    m = _ODP_DATA_RE.search(text)
    if m:
        d, mo_sl, y = m.groups()
        d = int(d)
        y = int(y)
        if 2000 <= y <= 2100 and 1 <= d <= 31:
            data_odpowiedzi = f"{y:04d}-{_MIESIACE.get(mo_sl.lower(), '00')}-{d:02d}"

    return {"radny": radny, "typ": typ,
            "data_wplywu": data_wplywu, "data_odpowiedzi": data_odpowiedzi}


def _pdf_links(html: str) -> list[str]:
    """Odsyłacze do plików PDF z sekcji (treść + odpowiedź)."""
    links = re.findall(r'href="([^"]*upload/pliki/[^"]+\.pdf[^"]*)"', html)
    out = []
    for u in links:
        if "download=1" in u:
            continue
        # względne -> absolutne
        if u.startswith("/"):
            u = "https://umlubartow.bip.lubelskie.pl" + u
        if u not in out:
            out.append(u)
    return out


def parse_record(row: dict, session, cache_dir) -> dict | None:
    pid = row.get("id_dokumentu")
    tresc = (row.get("tresc") or "").strip()
    data_utw = (row.get("data_utworzenia") or "").strip()
    bip_url = DETAIL.format(pid=pid)

    html = fetch_html(session, bip_url)
    if not html:
        print(f"  [skip] brak treści detail: {bip_url}")
        return None

    pdfs = _pdf_links(html)
    # Rozróżniamy treść i odpowiedź: plik "odpowiedz..." -> odpowiedź.
    tresc_url = ""
    odpowiedz_url = ""
    for u in pdfs:
        low = u.lower()
        if "odpowiedz" in low:
            if not odpowiedz_url:
                odpowiedz_url = u
        elif not tresc_url:
            tresc_url = u

    # Metadane z treści PDF (radny, typ, data wpływu) i odpowiedzi (data odp.).
    meta = {"radny": "", "typ": "interpelacja",
            "data_wplywu": "", "data_odpowiedzi": ""}
    if tresc_url:
        time.sleep(DELAY)
        meta.update(_extract_meta(_clean_text(fetch_bin(session, tresc_url))))
    if odpowiedz_url:
        time.sleep(DELAY)
        odp_meta = _extract_meta(_clean_text(fetch_bin(session, odpowiedz_url)))
        meta["data_odpowiedzi"] = odp_meta["data_odpowiedzi"]

    # rok z daty wpływu (albo z daty dodania do BIP)
    rok = 0
    if meta["data_wplywu"]:
        rok = int(meta["data_wplywu"][:4])
    elif data_utw[:4].isdigit():
        rok = int(data_utw[:4])

    kadencja = "2024-2029" if rok >= 2024 else "2018-2023"

    # cri: numer z tematu (np. "1/2026") albo id_dokumentu
    cri = ""
    m = re.search(r"(?:nr\s*)?(\d{1,3})(?:/\d{4})?", tresc, re.I)
    m2 = re.search(r"(?:nr|N[oó]\.?)\s*(\d{1,4})(?:\.?/)?\s*(\d{4})?", tresc, re.I)
    if m2:
        cri = m2.group(1)
    elif m and m.group(1) in ("1", "2", "3", "4", "5"):
        cri = m.group(1)
    else:
        cri = pid

    radny = meta["radny"]
    odpowiedz_status = "Udzielono" if odpowiedz_url else "Nie udzielono"

    # Sanity: odpowiedź nie może być wcześniejsza niż wpływu.
    if (meta["data_odpowiedzi"] and meta["data_wplywu"]
            and meta["data_odpowiedzi"] < meta["data_wplywu"]):
        meta["data_odpowiedzi"] = ""

    return {
        "cri": cri,
        "typ": meta["typ"],
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": tresc,
        "data_wplywu": meta["data_wplywu"],
        "klub": _club_for_radny(radny),
        "odpowiedz_status": odpowiedz_status,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": meta["data_odpowiedzi"],
        "bip_url": bip_url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Lubartowa"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też starsze (rok<2024); domyślnie tylko 2024+")
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Lubartów (Wrota Lubelszczyzny) ===")
    rows_all = []
    start = 0
    while True:
        time.sleep(DELAY)
        data = fetch_json(
            session,
            f"{REJESTR_AJAX}&iDisplayStart={start}&iDisplayLength={DAO}",
        )
        if not data:
            break
        rows = data.get("aaData") or []
        rows_all.extend(rows)
        total = int(data.get("iTotalRecords") or 0)
        start += len(rows)
        if not rows or start >= total:
            break
    print(f"  Rejestr: {len(rows_all)} dokumentów (razem {total or '?'})")

    records = []
    for i, row in enumerate(rows_all, start=1):
        rec = parse_record(row, session, args.cache_dir)
        if not rec:
            continue
        if not args.all and rec["rok"] and rec["rok"] < 2024:
            continue
        if not rec["rok"]:
            continue
        records.append(rec)
        if i % 10 == 0:
            print(f"  szczegóły: {i}...")

    # sortowanie: data wpływu desc, potem bid
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
