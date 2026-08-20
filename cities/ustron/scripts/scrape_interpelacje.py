#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Ustroń (IX kad. 2024-2029).

Źródło: BIP Ustronia (https://bip.ustron.pl, autorski CMS "artykul").

Listing (kategoria, paginowana `?page=N`):
    /artykuly/interpelacje   — interpelacje
    /artykuly/zapytania      — zapytania
  Każdy rekord to artykuł o ścieżce `/artykul/interpelacja-...` / `/artykul/zapytani...`.
  Tytuł artykułu zawiera radnego i przedmiot:
      "Interpelacja radnego Artura Steczkiewicza w sprawie ..."
      "Interpelacja radnej Kariny Wowry dotycząca ..."

Detal:
    Tytuł (h) -> radny + przedmiot.
    Metryczka załącznika:
        Wytworzył: Radny RM   |   Data wytworzenia: DD.MM.RRRR
    Załącznik (treść) -> /attachments/{id}/download/{nazwa} (np. "Interpelacja").
    Ustroń nie publikuje odpowiedzi -> odpowiedz_status = "Nie udzielono".

Klub radnego z config.json (fuzzy do mianownika). Dedupe po bip_url.
Rekordy z poprzednich kadencji (ARCHIWUM) nie wchodzą — kategoria i tak obejmuje
wyłącznie bieżącą kadencję.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.ustron.pl"
LIST_INTERP = f"{BASE}/artykuly/interpelacje"
LIST_ZAPYT = f"{BASE}/artykuly/zapytania"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.4
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


def _match_nominative(parsed):
    if not parsed:
        return ""
    best, best_ratio = "", 0.0
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else ""


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url, t=40):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=t, verify=False)
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


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


# radny z tytułu artykułu
# "Interpelacja radnego Artura Steczkiewicza w sprawie ..."
# "Interpelacja radnej Kariny Wowry dotycząca ...
# "Zapytanie radnego Artura Steczkiewicza dot. ..."
_RADNY_RE = re.compile(
    r"^(?:Interpelacja|Zapytanie)\s+radne(?:go|j)\s+(?P<radny>.+?)\s+"
    r"(?:w\s+sprawie\b|dotycz\w+|dot\.|–|-|:|\.)",
    re.I,
)


def _parse_title(title: str):
    """Zwraca (radny_raw, przedmiot)."""
    t = title.rstrip(".")
    m = _RADNY_RE.search(t)
    if m:
        radny = _clean(m.group("radny"))
        przedmiot = _clean(t[m.end():]) or t
        return radny, przedmiot
    return "", _clean(t)


def parse_listing(soup, typ):
    """Zwraca listę (href, radny_raw, przedmiot) w kolejności listingu."""
    out = []
    for a in soup.find_all("a", href=True):
        hr = a["href"]
        if "/artykul/interpelacja" in hr or "/artykul/zapytani" in hr:
            if not hr.startswith("http"):
                hr = BASE + hr
            title = _clean(a.get_text(" ", strip=True))
            radny_raw, przedmiot = _parse_title(title)
            out.append({"href": hr, "typ": typ, "radny": radny_raw, "przedmiot": przedmiot})
    return out


def _last_page(soup, base):
    last = 1
    basepath = re.sub(r"^https?://[^/]+", "", base)
    for a in soup.find_all("a", href=True):
        hr = a["href"]
        if basepath in hr:
            m = re.search(r"\?page=(\d+)", hr)
            if m:
                last = max(last, int(m.group(1)))
    return last


# metryczka: Data wytworzenia: DD.MM.RRRR
_METRIC_RE = re.compile(
    r"<th[^>]*>\s*Data wytworzenia:\s*</th>\s*<td[^>]*>\s*(\d{1,2})\.(\d{1,2})\.(\d{4})",
    re.S,
)
# załącznik (treść) -> /attachments/{id}/download/{nazwa}
_ATTACH_RE = re.compile(
    r'<a[^>]+href="(/attachments/\d+/download/[^"]+)"[^>]*>.*?'
    r'<span class="attachment-name-details__name">(.*?)</span>',
    re.S,
)


def _norm_date(d, mo, y):
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def detail_info(session, url, typ):
    html = fetch_text(session, url)
    if not html:
        return None
    # data wpływu z metryczki "Data wytworzenia"
    data_wplywu = ""
    rm = _METRIC_RE.search(html)
    if rm:
        data_wplywu = _norm_date(rm.group(1), rm.group(2), rm.group(3))
    # przedmiot z tytułu (h1/h2)
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for hd in soup.find_all(["h1", "h2", "h3"]):
        tt = _clean(hd.get_text(" ", strip=True))
        if tt and ("interpelacj" in tt.lower() or "zapytani" in tt.lower()):
            title = tt
            break
    radny_raw, przedmiot = _parse_title(title)
    # załączniki: treść = pierwszy o nazwie "Interpelacja"/"Zapytanie" (nie "Odpowiedź")
    tresc_url, odpowiedz_url = "", ""
    for href, name in _ATTACH_RE.findall(html):
        nlow = _clean(name).lower()
        if "odpowied" in nlow:
            if not odpowiedz_url:
                odpowiedz_url = (href if href.startswith("http") else BASE + href)
        elif "interpelacj" in nlow or "zapytani" in nlow or "wniosk" in nlow:
            if not tresc_url:
                tresc_url = (href if href.startswith("http") else BASE + href)
        elif not tresc_url:
            tresc_url = (href if href.startswith("http") else BASE + href)
    matched = _match_nominative(radny_raw) if radny_raw else ""
    radny = matched if matched else radny_raw
    klub = _club_for(matched) if matched else ""
    rok = 0
    try:
        rok = int(data_wplywu[:4]) if data_wplywu else 0
    except ValueError:
        rok = 0
    return {
        "cri": re.sub(r"\W+", "", url.rsplit("/", 1)[-1])[:40],
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": przedmiot or "",
        "data_wplywu": data_wplywu,
        "klub": klub,
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": url,
    }


def crawl_listing(session, base, typ, max_pages):
    print(f"  {typ}: {base}")
    html = fetch_text(session, base)
    if not html:
        print(f"    [skip] brak listingu dla {typ}")
        return []
    soup = BeautifulSoup(html, "html.parser")
    total = _last_page(soup, base)
    pages = min(total, max_pages) if max_pages else total
    print(f"    stron: {total}")
    seen = {}
    items = parse_listing(soup, typ)
    for it in items:
        seen[it["href"]] = it
    for page in range(2, pages + 1):
        time.sleep(DELAY)
        ph = fetch_text(session, f"{base}?page={page}")
        if not ph:
            continue
        for it in parse_listing(BeautifulSoup(ph, "html.parser"), typ):
            if it["href"] not in seen:
                seen[it["href"]] = it
    return list(seen.values())


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Ustroń (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — Ustroń (BIP) ===")
    items = []
    items += crawl_listing(session, LIST_INTERP, "interpelacja", args.max_pages)
    items += crawl_listing(session, LIST_ZAPYT, "zapytanie", args.max_pages)
    print(f"  rekordów w listingach: {len(items)}")

    records = []
    for it in items:
        info = detail_info(session, it["href"], it["typ"])
        if not info:
            print(f"    [skip] brak treści: {it['href']}")
            continue
        if info["rok"] and info["rok"] < MIN_ROK_DEFAULT:
            continue
        # pomiń całkowicie puste wpisy (placeholder bez radnego/przedmiotu/PDF-u)
        lbl = (info["przedmiot"] or "").strip().lower()
        empty_subject = lbl in ("interpelacja", "zapytanie", "wniosek", "interpelacje", "zapytania")
        if not info["radny"] and empty_subject and not info["tresc_url"]:
            print(f"    [pusty] bez danych: {info['bip_url']}")
            continue
        records.append(info)
        time.sleep(DELAY)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["bip_url"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    no_radny = sum(1 for r in records if not r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Bez radnego: {no_radny} | Razem: {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
