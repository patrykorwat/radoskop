#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Słupsku (IX kad. 2024-2029).

Źródło: BIP Słupska (https://bip.um.slupsk.pl), "Rada Miejska > Interpelacje, zapytania
i wnioski Radnych" (/rada_miejska/interpelacje/), lista paginowana (?pix=N), domyślnie
zakładka kadencji 2024-2029.

Struktura: każdy rekord = <div class="mx-list-item"> zawierający:
  * link referencji (np. "341/121/26") -> /rada_miejska/interpelacje/{id}.html,
  * "Interpelujący radny: {Imię Nazwisko}",
  * "dot. {przedmiot}",
  * 1..n linki plików (file/{id}) z etykietą:
      - interpelacja / zapytanie / wniosek  -> tresc_url,
      - odpowiedź                            -> odpowiedz_url,
      - załącznik ...                        -> pomijane.
  * metka: "Data wytworzenia: {DD miesiąc RRRR}" -> data_wplywu.

Typ z etykiety pliku; odpowiedz_status z obecności pliku "odpowiedź".
Radny dopasowywany fuzzy do config club_assignments. Dedupe po tresc_url.
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

BASE = "https://bip.um.slupsk.pl"
LIST_URL = f"{BASE}/rada_miejska/interpelacje/"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}


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
    return best if best_ratio >= 0.75 else name


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
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _max_pix(html):
    return max([int(p) for p in re.findall(r"\?pix=(\d+)", html)] + [0])


def parse_items(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.select("div.mx-list-item"):
        txt = _clean(it.get_text(" ", strip=True))
        refa = it.find("a", href=re.compile(r"interpelacje/\d+\.html"))
        ref = refa.get_text(" ", strip=True) if refa else ""
        bip_url = ""
        if refa:
            h = refa["href"]
            bip_url = h if h.startswith("http") else BASE + ("/" + h.lstrip("/"))
        radny_m = re.search(r"Interpelujący radny:\s*(.+?)\s+(?:dot\.|w sprawie)\s", txt)
        radny_raw = radny_m.group(1).strip() if radny_m else ""
        przedmiot = ""
        pm = re.search(r"(?:dot\.|w sprawie)\s+(.+)", txt)
        if pm:
            przedmiot = _clean(pm.group(1))
            przedmiot = re.split(r"\s+(?:interpelacja|zapytanie|wniosek|odpowiedź|załącznik|prolongata)\s+\(pdf", przedmiot, flags=re.I)[0]
            przedmiot = przedmiot.split("Pokaż metkę")[0].strip(" ,;")
        # metka: data wytworzenia
        dm = re.search(r"Data wytworzenia:\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", txt)
        data_wplywu = ""
        if dm:
            mon = _MONTHS.get(dm.group(2).lower())
            if mon:
                data_wplywu = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
        rok = int(data_wplywu[:4]) if data_wplywu else 0
        # pliki
        tresc_url, odpowiedz_url = "", ""
        typ = "interpelacja"
        for a in it.find_all("a", href=re.compile(r"file/\d+")):
            lab = _clean(a.get_text(" ", strip=True)).split("(")[0].strip().lower()
            h = a["href"]
            furl = h if h.startswith("http") else BASE + "/rada_miejska/interpelacje/" + h.lstrip("/")
            if "odpowiedź" in lab:
                if not odpowiedz_url:
                    odpowiedz_url = furl
            elif "załącznik" in lab or "prolongata" in lab:
                continue
            else:
                if not tresc_url:
                    tresc_url = furl
                l2 = lab
                if "zapytanie" in l2:
                    typ = "zapytanie"
                elif "wniosek" in l2:
                    typ = "wniosek"
        if not tresc_url:
            _log(f"  [slupsk] brak tresc_url: {ref or bip_url}")
            continue
        radny = _match_nominative(radny_raw)
        klub = _club_for(radny) if radny and radny in _CLUB_ASSIGN else ""
        cr = re.sub(r"[^0-9]", "-", ref).strip("-") or f"{rok}"
        out.append({
            "cri": f"cri-slupsk-{cr}",
            "typ": typ,
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
            "radny": radny,
            "przedmiot": przedmiot,
            "data_wplywu": data_wplywu,
            "klub": klub,
            "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
            "tresc_url": tresc_url,
            "odpowiedz_url": odpowiedz_url,
            "data_odpowiedzi": "",
            "bip_url": bip_url or LIST_URL,
        })
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Słupsk (BIP lista)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Słupsk (BIP lista paginowana) ===")
    html0 = fetch_text(session, LIST_URL)
    maxpix = _max_pix(html0)
    total_pages = maxpix + 1
    pages = min(total_pages, args.max_pages) if args.max_pages else total_pages
    print(f"  stron listingu: {total_pages} (przetwarzam {pages})")

    records = parse_items(html0)
    for pix in range(1, pages):
        time.sleep(DELAY)
        ph = fetch_text(session, f"{LIST_URL}?pix={pix}")
        if not ph:
            print(f"  [skip] pix={pix} brak treści")
            continue
        records.extend(parse_items(ph))

    seen, final = set(), []
    for r in records:
        if r["tresc_url"] in seen:
            continue
        seen.add(r["tresc_url"])
        final.append(r)
    if not args.all:
        final = [r for r in final if r["rok"] >= MIN_ROK_DEFAULT]
    records = sorted(final, key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    # zapewnij unikalność cri (wywołania z tym samym ref / brak refu)
    from collections import Counter
    cc = Counter(r["cri"] for r in records)
    used = {}
    for r in records:
        if cc[r["cri"]] > 1:
            n = used.get(r["cri"], 0) + 1
            used[r["cri"]] = n
            r["cri"] = f"{r['cri']}-{n}"

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
