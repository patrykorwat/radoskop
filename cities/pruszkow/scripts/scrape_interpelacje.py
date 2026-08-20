#!/usr/bin/env python3
"""Scraper interpelacji/zapytań/wniosków radnych Rady Miasta Pruszkowa.

Źródło: PRAWDZIWY BIP Pruszkowa — bip.um.pruszkow.pl (Logonet).
UWAGA: domena `bip.pruszkow.pl` z config.json jest ZABLOKOWANA/przejęta
(redirect do trackera reklamowego am-track.pl) — prawdziwy BIP to
bip.um.pruszkow.pl (link „BIP" z www.pruszkow.pl).

Rejestr „Interpelacje i zapytania":
  * Listing: /interpelacje/56, paginacja /interpelacje/{page}/{perPage}
    (perPage 5..25). Każdy rekord to osobna tabela:
        <th scope="row">Interpelacja w sprawie</th> -> <a href="/interpelacja/{id}/{slug}">{subject}</a>
        <th scope="row">Nr sprawy</th>        -> BRM.003.18.2026
        <th scope="row">Tożsamość radnego</th> -> radny (mianownik)
  * Detal (/interpelacja/{id}/{slug}): metryka th/td —
        Typ wystąpienia (Interpelacja/Zapytanie/Wniosek), Nr sprawy,
        Tożsamość radnego, w sprawie, Data wytworzenia (= data wpływu).
        Załączniki/odpowiedź: jak występuje, bierzemy z detalu.
  * Kadencja: rejestr zawiera kadencje 2010–2029; filtrujemy rok>=2024
    (IX kadencja 2024–2029).

Radny dopasowywany do config.json club_assignments (fuzzy diacritic, próg 0.72).
Autorzy zbiorowi (Klub/Komisja) zostają z klub="".

Output: format Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url,
data_odpowiedzi, bip_url}.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
"""

import argparse
import difflib
import html as htmllib
import json
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache, cached_fetch_text  # noqa: E402

BIP_BASE = "https://bip.um.pruszkow.pl"
REGISTER = f"{BIP_BASE}/interpelacje"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.7
MAX_PAGES = 120
PER_PAGE = 25
_DEBUG = False

# każdy rekord listingu to <div ><table>...</table></div>
_TABLE_RE = re.compile(r"<table[^>]*class=\"table table-borderless\"[^>]*>(.*?)</table>", re.S)
_TH_TD = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)


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


def _fuzzy_club_radny(name: str) -> str:
    if name in _CLUB_ASSIGN:
        return name
    best_key, best_score = "", 0.0
    for key in _CLUB_ASSIGN:
        s = difflib.SequenceMatcher(None, name.lower(), key.lower()).ratio()
        if s > best_score:
            best_score, best_key = s, key
    return best_key if best_score >= 0.72 else name


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _norm_date(d_m_y: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4}).*", (d_m_y or "").strip())
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _clean(s: str) -> str:
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_listing(html: str) -> list[dict]:
    """-> [{"bip_url","subject","cri","radny_raw","typ_raw"}]"""
    out = []
    for tbl in _TABLE_RE.findall(html):
        rec = {"bip_url": "", "subject": "", "cri": "", "radny_raw": ""}
        for th, td in _TH_TD.findall(tbl):
            label = _clean(th)
            val = _clean(td)
            if label == "Interpelacja w sprawie":
                m = re.search(r'href="(https://bip\.um\.pruszkow\.pl/interpelacja/\d+[^"]*)"', td)
                rec["bip_url"] = m.group(1) if m else ""
                rec["subject"] = val
            elif label == "Nr sprawy":
                rec["cri"] = val
            elif label == "Tożsamość radnego":
                rec["radny_raw"] = val
        if rec["bip_url"]:
            out.append(rec)
    return out


def parse_detail(html: str) -> dict:
    fields = {}
    for th, td in _TH_TD.findall(html):
        key = _clean(th).lower().rstrip(":").strip()
        fields[key] = _clean(td)
    typ_raw = fields.get("typ wystąpienia", "")
    typ = "interpelacja" if "interpelacj" in typ_raw.lower() else (
        "zapytanie" if "zapytan" in typ_raw.lower() else (
            "wniosek" if "wniosk" in typ_raw.lower() else (
                "interpelacja" if re.search(r"interpelacj", html, re.I) else "")))
    cri = fields.get("nr sprawy", "")
    radny = _fuzzy_club_radny(fields.get("tożsamość radnego", ""))
    przedmiot = fields.get("w sprawie", "")
    data = _norm_date(fields.get("data wytworzenia", ""))
    rok = int(data[:4]) if data else 0
    # odpowiedź: szukaj w detalu sekcji/załączników
    odp_url = ""
    m = re.search(r'href="([^"]*interpelacja[^"]*odpowied[^"]*)"', html, re.I)
    odp_url = m.group(1) if m else ""
    # tresc_url: sam detal (treść w HTML)
    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odp_url else "Nie udzielono",
        "tresc_url": "",
        "odpowiedz_url": odp_url,
        "data_odpowiedzi": "",
        "bip_url": "",
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji z BIP Pruszkowa")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else 2024

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje z BIP Pruszkowa ===")
    seen: dict[str, dict] = {}
    page = 1
    empty_streak = 0
    while page <= args.max_pages:
        url = f"{REGISTER}/{page}/{PER_PAGE}"
        html = cached_fetch_text(url, session=session, headers=HEADERS,
                                 timeout=30, delay=DELAY)
        rows = parse_listing(html)
        new = [r for r in rows if r["bip_url"] not in seen]
        if not new:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0
        for r in new:
            seen[r["bip_url"]] = r
        if page % 5 == 0 and not _DEBUG:
            print(f"  strona {page}... ({len(seen)} znalezionych)")
        page += 1
    print(f"  Listing: {len(seen)} unikalnych rekordów")

    records = []
    for item in seen.values():
        html = cached_fetch_text(item["bip_url"], session=session, headers=HEADERS,
                                 timeout=30, delay=DELAY)
        if not html:
            print(f"  [skip] brak treści: {item['bip_url']}")
            continue
        rec = parse_detail(html)
        rec["tresc_url"] = item["bip_url"]
        rec["bip_url"] = item["bip_url"]
        if not rec["przedmiot"]:
            rec["przedmiot"] = item["subject"]
        if not rec["cri"]:
            rec["cri"] = item["cri"]
        if not rec["radny"]:
            rec["radny"] = _fuzzy_club_radny(item["radny_raw"])
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        if not rec["rok"]:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    wni = sum(1 for r in records if r["typ"] == "wniosek")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}\nZapytania:     {zap}\nWnioski:       {wni}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
