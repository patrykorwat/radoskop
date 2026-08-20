#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Sulejówka (IX kad. 2024-2029).

Źródło: BIP Sulejówka (https://www.bip.sulejowek.pl), sekcja
"Interpelacje i zapytania Radnych" — kategorie roczne (Rok 2024 / 2025 / 2026),
każda = jedna strona z TABELĄ rekordów.

Struktura tabeli (7 kolumn):
  Lp | Skan interpelacji lub zapytania | Data złożenia (DD.MM.RRRR)
     | Imię i Nazwisko Radnego | Temat | Data przekazania Burmistrzowi | Odpowiedź

  * Skan (kol. 2) -> linki PDF interpelacji/zapytania (tresc_url; 1..2 duplikaty).
  * Odpowiedź (kol. 7) -> linki PDF odpowiedzi (odpowiedz_url).
  * Radny (kol. 4) -> dopasowanie fuzzy do config club_assignments.
  * Typ: z nazwy pliku PDF ("zapytanie"/"interpelacja"); referencje BRM/BBM go nie kodują.
  * data_wplywu z "Data złożenia"; data_odpowiedzi NIE jest publikowana w tabeli -> pusta.

Klub radnego z config.json (club_assignments -> clubs). Dedupe po tresc_url.
Rekordy < 2024 odrzucane (--all dla starszych).
"""
import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

BASE = "https://www.bip.sulejowek.pl"
# id kategorii rocznych w sekcji Interpelacje i zapytania Radnych
YEARS = {2026: "2379", 2025: "2238", 2024: "2153"}
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for(radny):
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_nominative(name):
    """Dopasowanie 'Imię Nazwisko' z tabeli do kanonicznego klucza config."""
    if not name:
        return ""
    name = name.strip()
    if name in _CLUB_ASSIGN:
        return name
    best, best_ratio = "", 0.0
    for cand in _CLUB_ASSIGN:
        r = SequenceMatcher(None, name.lower(), cand.lower()).ratio()
        if r > best_ratio:
            best_ratio, best = r, cand
    return best if best_ratio >= 0.7 else name


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=40)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_year_table(html, rok):
    """Parsuje tabelę rekordów dla danego roku."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(id=re.compile("PageContent")) or soup
    table = el.find("table") if el else None
    if table is None:
        _log(f"  [rok {rok}] brak tabeli")
        return []
    records = []
    rows = table.find_all("tr")
    for tr in rows[1:]:  # pomiń nagłówek
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        lp = _clean(cells[0].get_text(" ", strip=True))
        # Skan (tresc PDF)
        skan_cells = cells[1].find_all("a", href=True)
        tresc_url = ""
        for a in skan_cells:
            h = a["href"]
            if h.startswith("http"):
                tresc_url = h
                break
        else:
            if skan_cells:
                tresc_url = skan_cells[0]["href"]
        # Data złożenia DD.MM.RRRR
        data_raw = _clean(cells[2].get_text(" ", strip=True))
        d = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", data_raw)
        data_wplywu = ""
        if d:
            data_wplywu = f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}"
        rok_r = int(data_wplywu[:4]) if data_wplywu and data_wplywu[:4].isdigit() else rok
        radny_raw = _clean(cells[3].get_text(" ", strip=True))
        radny = _match_nominative(radny_raw)
        klub = _club_for(radny) if radny and radny in _CLUB_ASSIGN else ""
        przedmiot = _clean(cells[4].get_text(" ", strip=True))
        # Odpowiedź PDF
        odp_cells = cells[6].find_all("a", href=True)
        odpowiedz_url = ""
        for a in odp_cells:
            h = a["href"]
            if h.startswith("http"):
                odpowiedz_url = h
                break
        else:
            if odp_cells:
                odpowiedz_url = odp_cells[0]["href"]
        # Typ z nazwy pliku PDF
        typ = "interpelacja"
        probe = (tresc_url or "") + " " + (odpowiedz_url or "") + " " + _clean(cells[1].get_text(" ", strip=True))
        if re.search(r"zapytani", probe, re.I):
            typ = "zapytanie"
        if not tresc_url:
            _log(f"  [rok {rok}] rekord {lp} bez tresc_url — pomijam")
            continue
        ref_tokens = _clean(cells[1].get_text(" ", strip=True)).split()
        ref = ref_tokens[0] if ref_tokens else ""
        cri = f"cri-sulejowek-{ref or f'{rok_r}-{lp}'}"
        records.append({
            "cri": cri,
            "typ": typ,
            "rok": rok_r,
            "kadencja": "2024-2029" if rok_r >= 2024 else "2018-2024",
            "radny": radny,
            "przedmiot": przedmiot,
            "data_wplywu": data_wplywu,
            "klub": klub,
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": "",
            "bip_url": f"{BASE}/1178,interpelacje-i-zapytania-radnych",
        })
    return records


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Sulejówek (BIP tabela)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    years = sorted(YEARS, reverse=True) if args.all else [y for y in YEARS if y >= MIN_ROK_DEFAULT]
    print("=== Interpelacje — Sulejówek (BIP tabele roczne) ===")
    records = []
    for rok in years:
        url = f"{BASE}/{YEARS[rok]},rok-{rok}"
        html = fetch_text(session, url)
        if not html:
            print(f"  [rok {rok}] brak treści")
            continue
        recs = parse_year_table(html, rok)
        records.extend(recs)
        print(f"  rok {rok}: {len(recs)} rekordów")
        time.sleep(DELAY)

    # dedupe po tresc_url
    seen, final = set(), []
    for r in records:
        if r["tresc_url"] in seen:
            continue
        seen.add(r["tresc_url"])
        final.append(r)
    records = sorted(final, key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
