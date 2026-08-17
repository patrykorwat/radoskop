#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Bytomiu.

Źródło: BIP Bytom — kategoria "Interpelacje radnych".

Uwaga: eSesja Bytomia (bytom.esesja.pl/interpelacje_i_zapytania) jest
NIEAKTYWNA ("Brak aktywności lub moduł nieaktywny") — nie ma tam danych.
Prawdziwe źródło to BIP Bytom (bip.um.bytom.pl), system eBIP/Liferay,
który wymaga sesji DWR (SessionRefresh handshake) zanim zwróci treść.

Mechanizm:
  1. GET http://bip.um.bytom.pl/  -> JSESSIONID + refreshKey (w SessionRefresh.js)
  2. POST /dwr/exec  SessionRefresh.saveRefreshData({refreshId, refreshKey})
     (Content-Type: text/plain, DWR object encodowanie) -> zwraca true
  3. Listing: /bip/dokumenty?akcja=wyszukaj&idKategorii=76471&menuId=143843
     (10 dokumentów/stronę; paginacja ?akcja=zmienParametry&nrKrotki=N)
     Dla 'Interpelacje radnych' idKategorii=76471 / menuId=143843.
  4. Szczegóły: /bip/dokumenty/podglad?kod=<code> -> tytuł, streszczenie
     (przedmiot), 'odpowiedzialny za treść' (radny), data wytworzenia,
     typ (Interpelacje radnych), załączniki /zalacznik?idZalacznika=N.

Output: lista rekordów w schemacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/x
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all
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

BASE = "http://bip.um.bytom.pl"
KATEGORIA = "76471"
MENU = "143843"
LISTING_URL = f"{BASE}/bip/dokumenty"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
MAX_PAGES = 120
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
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers["Content-Type"] = "text/plain"
    return s


def bip_handshake(session: requests.Session) -> bool:
    """Wykonuje DWR SessionRefresh handshake i zwraca True gdy sukces."""
    try:
        r = session.get(f"{BASE}/", timeout=40)
        html = r.text
    except requests.RequestException as e:
        _log("handshake GET fail", e)
        return False
    # refreshKey/refreshId w SessionRefresh.js na stronie głównej
    m = re.search(r"refreshKey\s*=\s*'([A-F0-9]+)'", html, re.I)
    refresh_key = m.group(1) if m else ""
    m = re.search(r"refreshId\s*=\s*'([A-F0-9]+)'", html, re.I)
    refresh_id = m.group(1) if m else refresh_key
    if not refresh_key:
        _log("brak refreshKey na stronie głównej")
        return False
    body = (
        "callCount=1\n"
        "page=/bip/\n"
        "httpSessionId=\n"
        "scriptSessionId=\n"
        "c0-scriptName=SessionRefresh\n"
        "c0-methodName=saveRefreshData\n"
        "c0-id=0\n"
        "c0-param0=Object_Object:{{refreshId:reference:c0-e1,refreshKey:reference:c0-e2}}\n"
        "c0-e1=string:{refresh_id}\n"
        "c0-e2=string:{refresh_key}\n"
        "batchId=0\n"
        "instanceId=0\n"
        "page=\n"
        "windowName=\n"
    ).format(refresh_id=refresh_id, refresh_key=refresh_key)
    try:
        r = session.post(f"{BASE}/dwr/exec", data=body, timeout=40)
        ok = 'true' in r.text
        _log("handshake dwr/exec", r.status_code, 'true' in r.text)
        return ok
    except requests.RequestException as e:
        _log("handshake POST fail", e)
        return False


def fetch(session, url: str) -> str:
    time.sleep(DELAY)
    for attempt in range(3):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 200:
                return r.text
            _log(r.status_code, url)
            if r.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log("błąd", url, e)
            time.sleep(2)
    return ""


def parse_listing(html: str) -> list[str]:
    if not html:
        return []
    out = []
    for m in re.finditer(r"/bip/dokumenty/podglad\?kod=([a-z0-9.]+)", html):
        code = m.group(1)
        full = f"{BASE}/bip/dokumenty/podglad?kod={code}"
        if full not in out:
            out.append(full)
    return out


def parse_detail(html: str, url: str) -> dict | None:
    if not html:
        return None
    txt = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    txt = re.sub(r"[ \t]+", " ", txt)

    # Tytuł: <B>Interpelacja 739/2021 + odpowiedź</B>
    m = re.search(r"<B>\s*(Interpelacja|Zapytanie)\b(.*?)</B>", html, re.S | re.I)
    title = ""
    if m:
        title = re.sub(r"[ \t\r\n]+", " ", (m.group(1) + m.group(2))).strip()
    if not title:
        m = re.search(r"<B>(.*?)</B>", html, re.S | re.I)
        if m:
            title = re.sub(r"[ \t\r\n]+", " ", m.group(1)).strip()

    nr_m = re.search(r"(Interpelacja|Zapytanie)\s+([0-9]+)/([0-9]{4})", title, re.I)
    cri = ""
    rok = 0
    if nr_m:
        cri = f"{nr_m.group(2)}/{nr_m.group(3)}"
        rok = int(nr_m.group(3))

    # kategorie: np. 'Interpelacje radnych_->Interpelacje GAWENDA' (radny = nazwisko)
    kategorie = ""
    m = re.search(r"kategorie:\s*(.*?)(?:streszczenie:|źródło:|typ:|$)", txt, re.S | re.I)
    if m:
        kategorie = re.sub(r"\s+", " ", m.group(1)).strip()
    radny = ""
    m = re.search(r"Interpelacje\s+([A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż-]+)$", kategorie)
    if m:
        radny = m.group(1).strip()

    # streszczenie: (przedmiot)
    przedmiot = ""
    m = re.search(r"streszczenie:\s*(.*?)(?:źródło:|typ:|status:|$)", txt, re.S | re.I)
    if m:
        przedmiot = re.sub(r"\s+", " ", m.group(1)).strip()

    źródło = ""
    m = re.search(r"źródło:\s*(.*?)(?:typ:|status:|$)", txt, re.S | re.I)
    if m:
        źródło = re.sub(r"\s+", " ", m.group(1)).strip()
    if źródło and źródło not in ("Radny/Radna", "Radna/Radny") and not radny:
        radny = źródło

    # data: komentarz 'data uchwalenia / podpisania: 2021-01-29'
    data_wplywu = ""
    m = re.search(r"data (?:uchwalenia|wytworzenia|podpisania)[^:]*:\s*(\d{4}-\d{2}-\d{2})",
                  txt, re.S | re.I)
    if m:
        data_wplywu = m.group(1)
    if not data_wplywu and nr_m:
        # spadek: rok z numeru, dzień nieznany
        pass

    typ = "zapytanie" if re.search(r"zapytanie", (title or "") + (przedmiot or ""), re.I) else "interpelacja"
    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    # załączniki — '1. Treść' (treść), '2. Treść' (odpowiedź)
    tresc_url = ""
    odpowiedz_url = ""
    for href, label in re.findall(
        r'<a[^>]+href="([^"]*zalacznik[^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I
    ):
        lab = re.sub(r"<[^>]+>", " ", label).strip()
        hr = href if href.startswith("http") else BASE + href
        lm = re.search(r"(\d+)\.\s*Treść", lab)
        if lm:
            n = int(lm.group(1))
            if n == 1 and not tresc_url:
                tresc_url = hr
            elif n >= 2 and not odpowiedz_url:
                odpowiedz_url = hr

    odpowiedz_status = "Udzielono" if ("odpowiedź" in (title or "").lower() or odpowiedz_url) else "Nie udzielono"

    return {
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
        "bip_url": url,
    }


def normalize_date(d: str) -> str:
    d = (d or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", d)
    if m:
        y, mo, day = m.groups()
        return f"{y}-{int(mo):02d}-{int(day):02d}"
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", d)
    if m:
        dd, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(dd):02d}"
    return ""


def main() -> int:
    global _DEBUG, MIN_ROK
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Bytomia"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="Także VIII kadencja (2023 i wcześniej)")
    args = parser.parse_args()

    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT
    init_cache(args.cache_dir)

    session = _session()
    if not bip_handshake(session):
        print("[fatal] nie udało się nawiązać sesji BIP (DWR handshake)")
        return 1

    print("=== Interpelacje — BIP Bytom ===")
    seen: dict[str, str] = {}
    empty = 0
    page = 1
    n = 0  # nrKrotki offset (1-based first record on page)
    while page <= MAX_PAGES:
        if page == 1:
            url = f"{LISTING_URL}?akcja=wyszukaj&idKategorii={KATEGORIA}&menuId={MENU}"
        else:
            n = (page - 1) * 10 + 1
            url = f"{LISTING_URL}?akcja=zmienParametry&nrKrotki={n}"
        html = fetch(session, url)
        links = parse_listing(html)
        new = [u for u in links if u not in seen]
        _log(f"strona {page}: {len(links)} linków, nowych {len(new)}")
        if not new:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
        for u in new:
            seen[u] = ""
        if page % 10 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)})")
        page += 1
        # stop if a page has fewer than 10 (end of list)
        if 0 < len(links) < 10:
            break
    print(f"  Listing: {len(seen)} dokumentów")

    records = []
    # sesja DWR wygasa podczas długiego listingu — odśwież przed szczegółami
    bip_handshake(session)
    for i, url in enumerate(seen, 1):
        html = fetch(session, url)
        # error frame / zerwany handshake — odśwież sesję i spróbuj raz jeszcze
        if not html or "Gmina Bytom" not in html:
            bip_handshake(session)
            html = fetch(session, url)
        rec = parse_detail(html, url)
        if not rec:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        records.append(rec)
        if i % 50 == 0:
            print(f"  szczegóły: {i}...")

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Razem:        {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
