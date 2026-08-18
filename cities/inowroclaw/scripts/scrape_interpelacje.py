#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej Inowrocławia.

Źródło: BIP Inowrocławia (https://bip.inowroclaw.pl) — rejestr
"Interpelacje i zapytania kadencja 2024-2029":
    https://bip.inowroclaw.pl/artykuly/518/interpelacje-i-zapytania-kadencja-2024-2029

Struktura (CMS bip-gov typu artykuly/artykul):
  - rejestr /artykuly/518/...  listuje strony poszczególnych radnych:
        /artykuly/{cid}/{slug}   (cid: 519..541, 561)
  - każda strona radnego listuje jego interpelacje/zapytania jako artykuły-detałe:
        /artykul/{cid}/{iid}/{slug}
    tytuł: "Interpelacja z {D} {miesiąc} {RRRR} r. w sprawie {przedmiot}"
            | "Zapytanie z ..." | "Wniosek z ..."
  - detal ma załączniki:
        <a href="https://bip.inowroclaw.pl/attachments/download/{n}"> Interpelacja</a>  (treść)
        <a href="https://bip.inowroclaw.pl/attachments/download/{n}"> Odpowiedź</a>     (odpowiedź)

Klub radnego z config.json (club_assignments -> clubs).
Radni bez przypisania w config (np. Edmund Mikołajczak, Marcin Skonieczka) -> klub "".

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--max-pages N] [--max-records N]
"""

import argparse
import json
import re
import sys
import time
from html import unescape as _unescape
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.inowroclaw.pl"
REGISTER = f"{BASE}/artykuly/518/interpelacje-i-zapytania-kadencja-2024-2029"
MIN_ROK_DEFAULT = 2024  # bieżąca kadencja 2024-2029

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.35
_DEBUG = False

_PL_MONTH = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
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


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(4)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_title(title):
    """Z tytułu: (typ, data_wplywu, rok, przedmiot)."""
    t = _clean(title)
    typ = "interpelacja"
    low = t.lower()
    if low.startswith("zapytanie"):
        typ = "zapytanie"
    elif low.startswith("wniosek"):
        typ = "wniosek"
    # data: "z {D} {miesiąc} {RRRR} r."
    m = re.search(r"\bz\s+(\d{1,2})\s+(\S+?)\s+(\d{4})\s*r\.", t)
    data_wplywu, rok = "", 0
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        mm = _PL_MONTH.get(mon.lower())
        if mm:
            data_wplywu = f"{year}-{mm}-{int(day):02d}"
            rok = int(year)
    # przedmiot: po "w sprawie"
    przedmiot = t
    i = t.lower().find("w sprawie")
    if i >= 0:
        przedmiot = t[i + len("w sprawie"):].strip()
    return typ, data_wplywu, rok, _unescape(przedmiot)


def parse_detail(html, url):
    """Zwraca dict z detali (radny uzupełniany z kontekstu strony radnego)."""
    tresc_url, odpowiedz_url = "", ""
    # załączniki: <a href=".../attachments/download/{n}"> Label</a>
    for m in re.finditer(
        r'<a[^>]+href="(https://bip\.inowroclaw\.pl/attachments/download/\d+)"[^>]*>\s*([^<]{2,40}?)\s*</a>',
        html,
    ):
        label = _clean(m.group(2)).lower()
        href = m.group(1)
        if "odpowied" in label:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif "interpelac" in label or "zapytani" in label:
            if not tresc_url:
                tresc_url = href
        else:
            if not tresc_url:
                tresc_url = href
    return {"tresc_url": tresc_url, "odpowiedz_url": odpowiedz_url}


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Inowrocław (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = wszystkie (liczba radnych)")
    parser.add_argument("--max-records", type=int, default=0, help="0 = wszystkie")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Inowrocław (BIP bip.inowroclaw.pl) ===")

    # 1) rejestr -> radni (podmenu #submenu-518 listuje strony radnych)
    reg = fetch_text(session, REGISTER)
    if not reg:
        print("[FAIL] brak rejestru radnych")
        return 1
    councillors = []  # (cid, name) w kolejności rejestru
    sm = re.search(r'id="submenu-518"[^>]*>(.*?)</ul>', reg, re.S)
    submenu = sm.group(1) if sm else reg
    seen = set()
    for m in re.finditer(r'href="(https://bip\.inowroclaw\.pl/artykuly/(\d+)/([a-z0-9\-]+))"[^>]*>\s*([^<]{2,60}?)\s*</a>', submenu):
        cid = m.group(2)
        if cid in seen:
            continue
        name = _clean(m.group(4))
        seen.add(cid)
        councillors.append((cid, name))
    print(f"  radnych: {len(councillors)}")
    if args.max_pages:
        councillors = councillors[: args.max_pages]

    # 2) dla każdego radnego: lista interpelacji
    all_details = []  # (coun_name, detail_url)
    # Zbierz mapę id -> url radnego z rejestru
    cid_url = {}
    for m in re.finditer(r'href="(https://bip\.inowroclaw\.pl/artykuly/(\d+)/([a-z0-9\-]+))"', reg):
        cid_url.setdefault(m.group(2), m.group(1))

    for ci, (cid, cname) in enumerate(councillors, 1):
        purl = cid_url.get(cid)
        if not purl:
            continue
        page = 1
        while True:
            purl_page = purl if page == 1 else f"{purl}?Page={page}"
            ph = fetch_text(session, purl_page)
            if not ph:
                if page == 1:
                    print(f"  [skip] brak strony radnego {cname}")
                break
            dl = re.findall(r'href="(https://bip\.inowroclaw\.pl/artykul/%s/(\d+)/[^"]+)"' % cid, ph)
            dl = list(dict.fromkeys(dl))
            for url, iid in dl:
                all_details.append((cname, url))
            # czy jest następna strona
            nxt = re.findall(r'href="([^"]*artykuly/%s[^"]*\?Page=%d)"' % (cid, page + 1), ph)
            has_next = bool(nxt) or ('?Page=%d' % (page + 1)) in ph
            if not has_next:
                break
            page += 1
            if page > 20:
                break
        print(f"  radny {cname} ({cid}): obecnie {sum(1 for c, u in all_details if c == cname)} interpelacji")

    print(f"  łącznie detali: {len(all_details)}")

    # 3) detale
    records = []
    for i, (cname, url) in enumerate(all_details, 1):
        if args.max_records and i > args.max_records:
            break
        dh = fetch_text(session, url)
        if not dh:
            print(f"  [skip] brak treści {url}")
            continue
        att = parse_detail(dh, url)
        title = re.search(r"<h1[^>]*>(.*?)</h1>", dh, re.S) or \
                re.search(r"<title>(.*?)</title>", dh, re.S)
        title_text = _clean(title.group(1)) if title else ""
        typ, data_wplywu, rok, przedmiot = parse_title(title_text)
        if not rok or rok < MIN_ROK_DEFAULT:
            continue
        answered = bool(att["odpowiedz_url"])
        records.append({
            "cri": url.rstrip("/").split("/")[-2] if url.rstrip("/").split("/")[-1].isdigit() else url.rstrip("/").split("/")[-1],
            "typ": typ,
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
            "radny": cname,
            "przedmiot": przedmiot,
            "data_wplywu": data_wplywu,
            "klub": _club_for(cname),
            "odpowiedz_status": "Udzielono" if answered else "Nie udzielono",
            "tresc_url": att["tresc_url"],
            "odpowiedz_url": att["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": url,
        })
        if i % 25 == 0:
            print(f"  ... {i}/{len(all_details)}")

    # cri z porządkowego indeksu (detale bez stabilnego publicznego ID w URL)
    for n, r in enumerate(records, 1):
        if not r["cri"] or r["cri"].isdigit() is False:
            r["cri"] = str(n)
    # nadaj cri będący ostatnim segmentem URL, jeśli liczba
    for r in records:
        segs = r["bip_url"].rstrip("/").split("/")
        if segs[-1].isdigit():
            r["cri"] = segs[-1]

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)
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
