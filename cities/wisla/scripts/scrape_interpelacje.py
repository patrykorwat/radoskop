#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Wiśle (IX kad. 2024-2029).

Źródło: BIP Gminy Wisła (bip.wisla.pl, CMS "document-list" / jstree).
eSesja (wisla.esesja.pl) — moduł interpelacji NIEAKTYWNY ("Brak aktywności").

    https://bip.wisla.pl/lista/interpelacje-radnych

Struktura listingu (kategoria "Interpelacje radnych", pojedyncza strona):
  <article class="document-list"><ol><li><article>
      <header><p><span title="...">3 października 2024 08:36</span> | ...</p>
              <h3><a class="document-title" href="/interpelacja-...">Interpelacja Radnego X w sprawie ...</a></h3></header>
      <section class="lead"><h3>{tytuł}</h3><p>...</p></section>

  Detal:
      <h1>{tytuł}</h1>
      Pliki do pobrania: <li><a href="/zalacznik/{id}">Interpelacja Radnego X.pdf</a>
                          <li><a href="/zalacznik/{id}">Odpowiedź na interpelację ...pdf</a>
      Szczegółowe informacje:  Data wytworzenia: 3 października 2024 08:36

  Radny z tytułu ("Interpelacja Radnego/Radnej X w sprawie ..." / "Interpelacja w sprawie ...").
  tresc = załącznik "Interpelacja...", odpowiedz = załącznik "Odpowiedź na...".
Klub radnego z config.json (club_assignments -> clubs, fuzzy). Dedupe po href detalu.
Rekordy < 2024 odrzucane (--all dla starszych kadencji).

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

BASE = "https://bip.wisla.pl"
LIST_URL = f"{BASE}/lista/interpelacje-radnych"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
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


def _match_nominative(parsed):
    """'Nazwisko Imię' / 'Imię Nazwisko' -> kanoniczny klucz z config."""
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


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _parse_date(s):
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", s)
    if not m:
        _log(f"  [parse] brak daty w: {s!r}")
        return "", 0
    mo = _MONTHS.get(m.group(2).lower())
    if not mo:
        return "", 0
    rok = int(m.group(3))
    return f"{rok}-{mo:02d}-{int(m.group(1)):02d}", rok


_TITLE_RE = re.compile(
    r"^(?P<typ>interpelacj[ae]|zapytani[ae])\s+(?:radneg[oa]\s+)?(?P<radny>[^,]+?)\s+"
    r"w\s+sprawie\s+(?P<przedmiot>.+)$",
    re.I,
)

_TITLE_NO_RADNY = re.compile(r"^(?P<typ>interpelacj[ae]|zapytani[ae])\s+w\s+sprawie\s+(?P<przedmiot>.+)$", re.I)


def _parse_title(title):
    """Zwraca (typ, radny, klub, przedmiot)."""
    typ_default, radny, klub, przedmiot = "interpelacja", "", "", ""
    m = _TITLE_NO_RADNY.match(title)
    if m:
        typ_raw = m.group("typ").lower()
        typ = "zapytanie" if typ_raw.startswith("zapytani") else "interpelacja"
        return typ, "", "", m.group("przedmiot").strip()
    m = _TITLE_RE.match(title)
    if m:
        typ_raw = m.group("typ").lower()
        typ = "zapytanie" if typ_raw.startswith("zapytani") else "interpelacja"
        radny_raw = _clean(m.group("radny"))
        matched = _match_nominative(radny_raw)
        radny = matched if matched else radny_raw
        klub = _club_for(matched) if matched else ""
        return typ, radny, klub, m.group("przedmiot").strip()
    # fallback: cały tytuł jako przedmiot
    return typ_default, "", "", title.strip()


def parse_list_items(soup):
    items = []
    for li in soup.select("article.document-list ol > li"):
        a = li.select_one("a.document-title[href]")
        if not a:
            continue
        href = a.get("href").split("#")[0]
        title = _clean(a.get_text(" ", strip=True))
        hdr = li.select_one("header")
        date_span = hdr.select_one("span[title]") if hdr else None
        date_txt = _clean(date_span.get_text(" ", strip=True)) if date_span else ""
        data_wplywu, rok = _parse_date(date_txt)
        typ, radny, klub, przedmiot = _parse_title(title)
        items.append({
            "href": href, "typ": typ, "radny": radny, "klub": klub,
            "przedmiot": przedmiot, "data_wplywu": data_wplywu, "rok": rok,
            "title": title,
        })
    return items


_ATTACH_RE = re.compile(
    r"<a[^>]+href=[\"'](/zalacznik/\d+)[\"'][^>]*>(.*?)</a>", re.S | re.I
)


def detail_pdfs(session, url):
    """Zwraca (tresc_url, odpowiedz_url, odpowiedz_status)."""
    html = fetch_text(session, url)
    if not html:
        return "", "", "Nie udzielono"
    tresc_url, odpowiedz_url = "", ""
    for m in _ATTACH_RE.finditer(html):
        att_url, att_txt = m.group(1), _clean(m.group(2))
        if "odpowiedź" in att_txt.lower():
            if not odpowiedz_url:
                odpowiedz_url = BASE + att_url
        else:
            if not tresc_url:
                tresc_url = BASE + att_url
    status = "Udzielono" if odpowiedz_url else "Nie udzielono"
    return tresc_url, odpowiedz_url, status


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Wisła (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None, help="Nie dotyczy (1 strona)")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()
    min_rok = 0 if args.all else MIN_ROK_DEFAULT

    print("=== Interpelacje — Wisła (BIP) ===")
    html = fetch_text(session, LIST_URL)
    if not html:
        print("[fail] brak listingu")
        return 2
    soup = BeautifulSoup(html, "html.parser")
    items = parse_list_items(soup)
    print(f"  pozycji na liście: {len(items)}")

    records, seen = [], set()
    for it in items:
        if not it["rok"]:
            _log(f"  [skip] brak roku: {it['title'][:50]}")
            continue
        if it["rok"] < min_rok:
            _log(f"  [skip] rok {it['rok']} < {min_rok}")
            continue
        if it["href"] in seen:
            continue
        seen.add(it["href"])
        time.sleep(DELAY)
        tresc_url, odpowiedz_url, status = detail_pdfs(session, BASE + it["href"])
        # data / przedmiot popraw z detalu, jeśli listing nie wystarczył
        rec = {
            "cri": it["href"], "typ": it["typ"], "rok": it["rok"],
            "kadencja": "2024-2029" if it["rok"] >= 2024 else "",
            "radny": it["radny"], "przedmiot": it["przedmiot"] or it["title"],
            "data_wplywu": it["data_wplywu"], "klub": it["klub"],
            "odpowiedz_status": status,
            "tresc_url": tresc_url or "", "odpowiedz_url": odpowiedz_url or "",
            "data_odpowiedzi": "", "bip_url": BASE + it["href"],
        }
        records.append(rec)
        print(f"  [{len(records)}] {it['typ']} {it['rok']} {it['radny'] or '(bez radnego)'}: {rec['przedmiot'][:50]}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  zapisano {len(records)} rekordów -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
