#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta w Łańcucie.

Źródło: BIP Łańcuta na platformie Logonet (lancut.biuletyn.net), kategoria
"Interpelacje i zapytania radnych" (główna cid=699) z podkategoriami rocznymi:
    2026 -> cid=1146, 2025 -> cid=1080, 2024 -> cid=1033
eSesja (https://lancut.esesja.pl/interpelacje_i_zapytania) — moduł NIEAKTYWNY
("Brak aktywności lub moduł nieaktywny"), stąd źródłem jest BIP.

Struktura:
  * Listing (per rok): ?bip=2&cid={cid}&pg={n} — linki do artykułów
        ?bip=2&cid={cid}&id={id}
  * Detal (?bip=2&cid={cid}&id={id}):
        - <h2 class="aktualnosci-tytul">: "Treść zakładki Interpelacja radnego
          X z dnia DD.MM.YYYY" / "Zapytanie radnego X znak sprawy Y z dn. DD.MM.YYYY"
        - <div class="aktualnosci-tresc">: linki do PDF (załączniki)
            . odpowiedź: label "Odpowiedź Burmistrza Miasta Łańcuta na ..."
            . treść: label zaczynający się od "Interpelacja"/"Zapytanie"
Kluby radnych z config.json (club_assignments -> clubs). Radny w tytule zapisany
jest w formie dopełniacza ("Pawła Ciska"), mapujemy go do mianownika z config
przez fuzzy-match sufiksu (nazwisko), tak samo jak w kanonicznych scraperach.

Output: rekordy w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/interp/lancut
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://lancut.biuletyn.net"
# Rok -> cid podkategorii "Interpelacje i zapytania radnych"
YEAR_CIDS = {
    2026: "1146",
    2025: "1080",
    2024: "1033",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.5
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029

_DEBUG = False
_CLUB_ASSIGN = {}
_CLUBS = {}


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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _club_for_radny(radny: str) -> str:
    """Klub po dokładnym dopasowaniu mianownika z club_assignments."""
    code = _CLUB_ASSIGN.get((radny or "").strip(), "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_radny(genitive: str) -> str:
    """Mapuje dopełniaczowe imię+nazwisko z tytułu na mianownik z config.

    Tytuł podaje np. "Pawła Ciska" (dopełniacz) — config ma "Paweł Cisek".
    Używa fuzzy-match na nazwisku (ostatni segment), próg ~0.72.
    Zwraca kanoniczny klucz config lub oryginał.
    """
    import difflib

    g = _norm(genitive)
    if not g:
        return g
    keys = list(_CLUB_ASSIGN.keys())
    if not keys:
        return g
    # dokładny hit
    if g in _CLUB_ASSIGN:
        return g
    # po nazwisku (ostatni segment), uwzględnij też imię
    cands = []
    for k in keys:
        r1 = difflib.SequenceMatcher(None, g, k).ratio()
        r2 = 0.0
        kname = k.split()[-1] if k.split() else k
        gname = g.split()[-1] if g.split() else g
        r2 = difflib.SequenceMatcher(None, gname, kname).ratio()
        cands.append((max(r1, r2), k))
    cands.sort(reverse=True)
    best_score, best_key = cands[0]
    if best_score >= 0.72:
        return best_key
    return g


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _abs(url: str) -> str:
    if url.startswith("http"):
        return url
    return BASE + "/" + url.lstrip("/")


def fetch_text(session: requests.Session, url: str) -> str:
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_LISTING_RE = re.compile(r"\?bip=2&amp;cid=(\d+)&amp;id=(\d+)")


def parse_listing(html: str, cid: str) -> list[int]:
    if not html:
        return []
    out = []
    for c, i in _LISTING_RE.findall(html):
        if c == cid and int(i) not in out:
            out.append(int(i))
    return out


_DETAIL_TITLE_RE = re.compile(
    r'<h2[^>]*class="[^"]*aktualnosci-tytul[^"]*"[^>]*>(.*?)</h2>', re.S
)
_ATT_RE = re.compile(r'<a href="(fls/bip_pliki/[^"]+\.pdf)">(.*?)</a>', re.S)


def _title_type_and_radny(title_raw: str):
    """Z surowego tytułu (bez prefiksu 'Treść zakładki') -> (typ, radny_gen, data, przedmiot)."""
    t = re.sub(r"<[^>]+>", " ", title_raw)
    t = re.sub(r"\s+", " ", t).strip()
    # usuń ewentualny prefiks "Treść zakładki"
    t = re.sub(r"^Treść zakładki\s*", "", t, flags=re.I)
    low = t.lower()

    if "interpelacja" in low:
        typ = "interpelacja"
    elif "zapytanie" in low:
        typ = "zapytanie"
    else:
        typ = "interpelacja"

    # radny: "Interpelacja radnego X z dnia..." / "Zapytanie radnego/radnej X znak sprawy..."
    # radnego (m) / radnej (f) + dopełniacz imienia+nazwiska
    rm = re.search(
        r"radn(?:eg[oai]|ej)\s+(.+?)\s+(?:z dnia|znak sprawy|z\s+dn\.)", t, re.I
    )
    radny_gen = ""
    if rm:
        radny_gen = _norm(rm.group(1))
        radny_gen = re.sub(r"\s*(\.pdf)?$", "", radny_gen)

    # data: ostatnia DD.MM.YYYY albo "z dnia 26.02.2024" / "z dn. 20.07.2026"
    # (dopuszcza opcjonalną spację "19. 2024" — literówka w 1 rekordzie 2024)
    dm = re.findall(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", t)
    data = ""
    if dm:
        d, mo, y = dm[-1]
        data = f"{y}-{int(mo):02d}-{int(d):02d}"

    return typ, radny_gen, data, t


def parse_detail(html: str, url: str) -> dict | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    tt = soup.find("h2", class_="aktualnosci-tytul")
    title_raw = str(tt) if tt else ""
    typ, radny_gen, data_wplywu, przedmiot = _title_type_and_radny(title_raw)

    radny = _match_radny(radny_gen)

    # załączniki: (href, label)
    atts = []
    for href, label in _ATT_RE.findall(html):
        lbl = re.sub(r"<[^>]+>", " ", label)
        lbl = re.sub(r"\s+", " ", lbl).strip()
        atts.append((_abs(href), lbl))

    tresc_url = ""
    odpowiedz_url = ""
    data_odpowiedzi = ""
    for href, lbl in atts:
        low = lbl.lower()
        is_answer = ("odpowiedź" in low) or ("odpowiedz" in low) or ("wyjaśnienie" in low) \
            or ("odpowiedź burmistrza" in low) or ("odpowiedz burmistrza" in low)
        # również po nazwie pliku
        if not is_answer and "odpowiedz" in href.lower():
            is_answer = True
        if is_answer:
            if not odpowiedz_url:
                odpowiedz_url = href
                # spróbuj wyciągnąć datę odpowiedzi z labela (np. "z dn. 25.06.2026 r.")
                adm = re.findall(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", low)
                if adm:
                    d, mo, y = adm[-1]
                    data_odpowiedzi = f"{y}-{int(mo):02d}-{int(d):02d}"
        else:
            if not tresc_url and ("interpelacj" in low or "zapytan" in low or "wniosk" in low):
                tresc_url = href
    # fallback treść: pierwszy załącznik, który nie jest odpowiedzią
    if not tresc_url:
        for href, lbl in atts:
            low = lbl.lower()
            if not ("odpowiedź" in low or "odpowiedz" in low):
                tresc_url = href
                break

    # rok i cri
    m = re.search(r"id=(\d+)", url)
    cri = m.group(1) if m else ""
    rok = 0
    if data_wplywu:
        try:
            rok = int(data_wplywu[:4])
        except ValueError:
            rok = 0

    kadencja = "2024-2029" if rok >= 2024 else ("2018-2024" if rok >= 2018 else "")

    return {
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
        "data_odpowiedzi": data_odpowiedzi,
        "bip_url": url,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych — Rada Miasta Łańcuta (lancut.biuletyn.net)"
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

    init_cache(args.cache_dir)
    _load_clubs()
    session = _session()

    print("=== Interpelacje / Zapytania — Rada Miasta Łańcuta (Logonet BIP) ===")

    years = sorted(YEAR_CIDS.keys()) if args.all else [y for y in YEAR_CIDS if y >= MIN_ROK_DEFAULT]

    seen: dict[str, str] = {}
    for year in years:
        cid = YEAR_CIDS[year]
        for pg in range(0, 40):
            lst_url = f"{BASE}/?bip=2&cid={cid}&pg={pg}"
            html = fetch_text(session, lst_url)
            ids = parse_listing(html, cid)
            new = [i for i in ids if f"{cid}/{i}" not in seen]
            _log(f"  {year} pg={pg}: {len(ids)} art, nowe {len(new)}")
            for i in new:
                seen[f"{cid}/{i}"] = (
                    f"{BASE}/?bip=2&cid={cid}&id={i}"
                )
            if not new and pg > 3:
                break
    print(f"  Listing: {len(seen)} artykułów (lata {years})")

    records = []
    for key in seen:
        url = seen[key]
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści: {url}")
            continue
        rec = parse_detail(html, url)
        if not rec:
            continue
        if rec["rok"] and rec["rok"] < MIN_ROK_DEFAULT:
            continue
        # IX kadencja zaczyna się 2024-05-07 — odrzuć nieliczne wpisy z jej
        # poprzedniczki (jeszcze w roku 2024), gdy nie użyto --all.
        if not args.all and rec["data_wplywu"] and rec["data_wplywu"] < "2024-05-07":
            continue
        records.append(rec)
        _log(f"  {rec['cri']} {rec['typ']} {rec['data_wplywu']} {rec['radny']}")

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
