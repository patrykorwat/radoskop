#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Imielinie.

Źródło: BIP Imielina (https://bip.imielin.pl) — rejestr
"Interpelacje i zapytania Radnych":
    https://bip.imielin.pl/pl/1533/0/interpelacje-i-zapytania-radnych.html
(jedna strona rejestru; każdy rekord = artykuł /pl/1533/{id}/{slug}.html)

eSesja (https://imielin.esesja.pl/interpelacje_i_zapytania) — moduł NIEAKTYWNY,
źródłem jest rejestr BIP.

Detal /pl/1533/{id}/{slug}.html:
    <title> = "Interpelacja Radnego {Imię Nazwisko} {Nr/nr [... z dnia ...]}" —
              tu radny, typ i datę bierzemy z tytułu.
    data_wplywu z frazy "... z dnia 26 stycznia 2026 r." (polskie miesiące)
             lub z daty publikacji artykułu (DD.MM.RRRR).
    Załączniki w "Pliki do pobrania": treść (PDF), odpowiedź (PDF,
             zwykle "...dot.-interpelacji..." / "odpowiedź...").

Klub radnego z config.json (club_assignments -> clubs).
BIP serwuje certyfikat bez zaufanego CA — requests z verify=False.

Output: rekordy w formacie Radoskop.
Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--debug]
"""

import argparse
import json
import re
import sys
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.imielin.pl"
REGISTER = f"{BASE}/pl/1533/0/interpelacje-i-zapytania-radnych.html"
MIN_ROK_DEFAULT = 2024
_VERIFY_TLS = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.35
_DEBUG = False

_MIESIACE = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
}

from difflib import SequenceMatcher


def _match_nominative(parsed):
    """Fuzzy-dopasowuje formę z BIP (gen. 'Artura Olesia') do klucza config (mianownik)."""
    best, best_ratio = "", 0.0
    p = parsed.lower()
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, p, name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else parsed


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
            resp = session.get(url, timeout=30, verify=_VERIFY_TLS)
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


# Linki do artykułów-детali w rejestrze
_DETAIL_RE = re.compile(r'href="(/pl/1533/(\d+)/[^"]+\.html)"')


def parse_listing(html):
    out = []
    for m in _DETAIL_RE.finditer(html):
        href, rid = m.group(1), m.group(2)
        url = href if href.startswith("http") else BASE + href
        if rid == "0":
            continue  # to strona rejestru, nie detal
        out.append((int(rid), url))
    # dedupe po url (każdy detal występuje 2x — link + przycisk)
    seen = set()
    uniq = []
    for rid, url in out:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((rid, url))
    return uniq


def _parse_date_from_slug(title):
    """'... z dnia 26 stycznia 2026 r.' -> 2026-01-26"""
    m = re.search(r"z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", title, re.I)
    if m:
        d, mon, y = m.group(1), m.group(2).lower(), m.group(3)
        if mon in _MIESIACE:
            return f"{y}-{_MIESIACE[mon]}-{int(d):02d}"
    m2 = re.match(r".*?(\d{2})\.(\d{2})\.(\d{4})", title)
    if m2:
        d, mo, y = m2.groups()
        return f"{y}-{mo}-{d}"
    return ""


def parse_detail(html, title):
    # radny: "Radnego {Imię Nazwisko}" / "Radnej {I N}" / "Radnych: A i B, C"
    m = re.search(
        r"interpelacj[a-z]*\s+(?:zapytani[ae]|radneg[oa]|radnych|radn[a-z]*)\s*:?\s*"
        r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+)+)",
        title, re.I)
    radny = ""
    if m:
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        # przy współautorach bierzemy pierwszego (rozłącznik ' i ' / ', ')
        raw = re.split(r"\s+i\s+|\s*,\s*", raw)[0].strip()
        radny = _match_nominative(raw)

    low = title.lower()
    if low.startswith("zapytani"):
        typ = "zapytanie"
    elif low.startswith("wniosek"):
        typ = "wniosek"
    else:
        typ = "interpelacja"

    data_wplywu = _parse_date_from_slug(title)
    if not data_wplywu:
        # fallback: data publikacji artykułu DD.MM.RRRR w treści
        m3 = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", re.sub(r"<[^>]+>", " ", html))
        if m3:
            data_wplywu = f"{m3.group(3)}-{m3.group(2)}-{m3.group(1)}"

    # pliki do pobrania: treść + odpowiedź
    tresc_url, odpowiedz_url = "", ""
    for a in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = a[0]
        label = _clean(a[1]).lower()
        if "/mfiles/" not in href and not re.search(r"\.(pdf|docx?|odt)", href, re.I):
            continue
        if not href.startswith("http"):
            href = BASE + href
        if "odpowied" in label or "dot.-" in href or "dot_-" in href or "odp." in href:
            if not odpowiedz_url:
                odpowiedz_url = href
        else:
            if not tresc_url:
                tresc_url = href

    rok = int(data_wplywu[:4]) if data_wplywu and data_wplywu[:4].isdigit() else MIN_ROK_DEFAULT
    return {
        "typ": typ,
        "radny": radny,
        "przedmiot": title,
        "data_wplywu": data_wplywu,
        "rok": rok,
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Imielin (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Imielin (BIP bip.imielin.pl) ===")
    html = fetch_text(session, REGISTER)
    if not html:
        print("  [skip] brak treści rejestru")
        return 1
    items = parse_listing(html)
    print(f"  rekordów w rejestrze: {len(items)}")

    records = []
    for i, (rid, url) in enumerate(items, 1):
        dhtml = fetch_text(session, url)
        if not dhtml:
            print(f"  [skip] brak treści {url}")
            continue
        mt = re.search(r"<title>(.*?)</title>", dhtml, re.S)
        title = _clean(mt.group(1)).split(" - ")[0].strip() if mt else ""
        if not title:
            title = url.split("/")[-1].replace(".html", "").replace("-", " ")
        d = parse_detail(dhtml, title)
        if d["rok"] < MIN_ROK_DEFAULT:
            continue
        records.append({
            "cri": str(rid),
            "typ": d["typ"],
            "rok": d["rok"],
            "kadencja": "2024-2029" if d["rok"] >= 2024 else "2018-2024",
            "radny": d["radny"],
            "przedmiot": d["przedmiot"],
            "data_wplywu": d["data_wplywu"],
            "klub": _club_for(d["radny"]),
            "odpowiedz_status": d["odpowiedz_status"],
            "tresc_url": d["tresc_url"],
            "odpowiedz_url": d["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": url,
        })
        if i % 10 == 0:
            print(f"  ... {i}/{len(items)}")

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
