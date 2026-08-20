#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Piastowie (IX kad. 2024-2029).

Źródło: BIP Piastowa (https://bip.piastow.pl), rejestr
"Interpelacje i zapytania -> Kadencja 2024-2029" (lista /lista/kadencja-2024-2029-8).

Struktura (autorski CMS, domenowo-bazowy):
  * Listing = jedna strona listy z linkami do detali:
      <a href="/interpelacja-radnego-{slug}">Interpelacja radnego X</a>
      <a href="/zapytanie-radnego-{slug}">Zapytanie radnego X</a>
  * Detal = /interpelacja-radnego-{slug} (lub /zapytanie-...):
      - <article class="lead"><p>dot.: {przedmiot}</p></article>  (przedmiot po "dot.:")
      - <h2>{tytuł}</h2>  — tytuł koduje typ + autora w dopełniaczu
      - załączniki <ul class="attach_show_list">: <a href="/zalacznik/{id}">{filename}.pdf</a>
        filename "Interpelacja radnego G. Szuplewskiego z dn. 10.07.2026 r..pdf"
        oraz "Odpowiedź Burmistrza ... z dn. ..." -> odpowiedz_url.
      - <span title="...">X miesiąca YYYY HH:MM</span> (historia) — data publikacji.

Data wpływu z nazwy załącznika ("z dn. DD.MM.YYYY"). Status odpowiedzi = obecność
załącznika "Odpowiedź". Radny dopasowywany do config.json club_assignments przez
fuzzy (surname-stem) dopasowanie dopełniacza do mianownika; autorzy zbiorowi
("Klub ...") bez radnego -> klub="".

Output: rekordy w schemacie Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}.
"""

import argparse
import difflib
import html as HH
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

LISTING_URL = "https://bip.piastow.pl/lista/kadencja-2024-2029-8"
BIP_BASE = "https://bip.piastow.pl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.55

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


def _stem(name: str) -> str:
    name = re.sub(r"[^a-ząćęłńóśźż\s]", "", name.lower()).strip()
    if not name:
        return ""
    return "".join(w[:6] for w in name.split())


def _fuzzy_radny(gen: str) -> str:
    """Dopełniacz -> kanoniczny mianownik radnego z club_assignments (fuzzy)."""
    g = gen.strip()
    if not g:
        return ""
    if g in _CLUB_ASSIGN:
        return g
    gs = _stem(g)
    best_key, best = "", 0.0
    for key in _CLUB_ASSIGN:
        ks = _stem(key)
        score = difflib.SequenceMatcher(None, gs, ks).ratio()
        g_last = _stem(g.split()[-1])
        k_last = _stem(key.split()[-1]) if key.split() else ""
        slast = difflib.SequenceMatcher(None, g_last, k_last).ratio()
        s = max(score, slast)
        if s > best:
            best, best_key = s, key
    return best_key if best >= 0.55 else gen


def _radny_from_title(title: str) -> str:
    """Dopełniacz autora z tytułu np. 'Interpelacja radnego Grzegorza
    Szuplewskiego'. Autorzy zbiorowi (Klub ...) -> ''."""
    t = HH.unescape(title)
    # zbieramy frazę po (Interpelacja|Zapytanie|Wniosek)
    m = re.search(r"\b(?:Interpelacja|Zapytanie|Wniosek)\b\s*(.*)$", t, re.I)
    if not m:
        return ""
    author = m.group(1).strip()
    # usuń prefiks rodzajnika (radnego/radnej/radnych/radny/radni)
    author = re.sub(r"^\s*(?:radnego|radnej|radnych|radnego|radny|radnych|radni)\s+", "", author, flags=re.I)
    # utnij frazy po danej osobie (do "dot.", "w sprawie", "ws." itd.)
    author = re.split(r"\s+(?:dot\.|w sprawie|ws\.|dotycz)\b", author)[0].strip()
    # autorzy zbiorowi: Klub ...
    if re.search(r"\b[Kk]lub\b", author):
        return ""
    author = author.strip(" ,;:-")
    if not author:
        return ""
    return _fuzzy_radny(author)


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url, delay=True):
    for attempt in range(3):
        try:
            if delay:
                time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            _log("  %s %s" % (resp.status_code, url))
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log("  błąd %s: %s" % (url, e))
            time.sleep(2)
    return ""


_LINK_RE = re.compile(
    r'<a[^>]+href="(/interpelacja[^"#]*|/zapytanie[^"#]*)"[^>]*>(.*?)</a>',
    re.S,
)


def parse_listing(html: str) -> list[dict]:
    out = []
    seen = set()
    for href, label in _LINK_RE.findall(html):
        if href in seen:
            continue
        seen.add(href)
        label = HH.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label))).strip()
        out.append({"href": BIP_BASE + href, "title": label})
    return out


_LEAD_RE = re.compile(r'<article class="lead">\s*<p[^>]*>(.*?)</p>\s*</article>', re.S)
_ATTACH_RE = re.compile(
    r'<a aria-label="Pobierz załącznik: ([^"]+)"[^>]*href="([^"]+)"', re.S
)
_DATE_FROM_FNAME_RE = re.compile(r"z dn\.\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)


def parse_detail(html: str, item: dict) -> dict | None:
    if not html:
        return None
    # tytuł z h2 zawierającego Interpelacja/Zapytanie/Wniosek (pomija nagłówek strony)
    m = None
    for hm in re.finditer(r"<h2[^>]*>(.*?)</h2>", html, re.S):
        cand = HH.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", hm.group(1)))).strip()
        if re.search(r"\b(?:Interpelacja|Zapytanie|Wniosek)\b", cand, re.I):
            m = cand
            break
    title = m if m else item["title"]
    title_l = title.lower()
    if "zapytanie" in title_l:
        typ = "zapytanie"
    elif "interpelacja" in title_l:
        typ = "interpelacja"
    else:
        typ = "interpelacja"

    # przedmiot z lead ("dot.: ...")
    lm = _LEAD_RE.search(html)
    przedmiot = ""
    if lm:
        przedmiot = HH.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lm.group(1)))).strip()
        przedmiot = re.sub(r"^dot\.:\s*", "", przedmiot, flags=re.I).strip()

    radny = _radny_from_title(title)

    # załączniki
    tresc_url, odpowiedz_url = "", ""
    data_wplywu = ""
    for fname, href in _ATTACH_RE.findall(html):
        fname = HH.unescape(fname)
        url = href if href.startswith("http") else BIP_BASE + href
        low = fname.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = url
        elif not tresc_url:
            tresc_url = url
        # data wpływu z nazwy załącznika interpelacji
        dm = _DATE_FROM_FNAME_RE.search(fname)
        if dm and not data_wplywu:
            data_wplywu = "%s-%02d-%02d" % (dm.group(3), int(dm.group(2)), int(dm.group(1)))
    # fallback: data z pierwszej nazwy załącznika
    if not tresc_url:
        for fname, href in _ATTACH_RE.findall(html):
            tresc_url = href if href.startswith("http") else BIP_BASE + href
            break

    rok = int(data_wplywu[:4]) if data_wplywu else 0

    # data odpowiedzi z nazwy załącznika odpowiedzi
    data_odp = ""
    if odpowiedz_url:
        for fname, href in _ATTACH_RE.findall(html):
            fl = fname.lower()
            if "odpowied" in fl:
                dm = _DATE_FROM_FNAME_RE.search(fname)
                if dm:
                    data_odp = "%s-%02d-%02d" % (dm.group(3), int(dm.group(2)), int(dm.group(1)))
                    break

    return {
        "cri": "",
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": data_odp,
        "bip_url": item["href"],
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji/zapytań Piastów")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else 2024

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Piastów (lista kadencja 2024-2029) ===")
    html = fetch_text(session, LISTING_URL, delay=False)
    items = parse_listing(html)
    print("  Listing: %d rekordów" % len(items))

    records = []
    for item in items:
        detail_html = fetch_text(session, item["href"])
        if not detail_html:
            print("  [skip] brak treści: %s" % item["href"])
            continue
        rec = parse_detail(detail_html, item)
        if not rec:
            continue
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["bip_url"]))
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print("Interpelacje:  %d" % interp)
    print("Zapytania:     %d" % zap)
    print("Z odpowiedzią: %d" % answered)
    print("Razem:         %d" % len(records))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Zapisano: %s (%.1f KB)" % (out, out.stat().st_size / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
