#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Wałbrzych (IX kad. 2024-2029).

Źródło: BIP Urzędu Miejskiego (bip.um.walbrzych.pl) — moduł "Interpelacje i zapytania".

    Listing:  https://bip.um.walbrzych.pl/interpelacje/{page}/10   (10/page, 78 stron IX kad.)
        każdy rekord = <a href="/interpelacja/{id}/{slug}">{przedmiot}</a>
        slug zawiera nr+rok np. "54-26-m-kalinowski-interpelacja..." (nr 54 / 2026)

    Detal:    https://bip.um.walbrzych.pl/interpelacja/{id}/{slug}
        Tytuł:      "54/26 M. Kalinowski Interpelacja w sprawie ..."
        Tabela:     Typ wystąpienia | Nr sprawy | Tożsamość radnego | w sprawie <przedmiot>
        Załączniki: "interpelacja"/"zapytanie" pdf (tresc) + ewentualnie "odpowiedź" pdf
        Metryczka:  Data wytworzenia DD.MM.RRRR -> data_wplywu

Klub radnego z config.json (club_assignments -> clubs); radnego dopasowujemy po
INICJALE + nazwisku (źródło podaje "M. Kalinowski"), unikalnym w configu Wałbrzycha;
fallback - fuzzy do mianownika.

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

from http_cache import init_cache, cached_fetch_text  # noqa: E402

# TTL cache dla stron szczegółowych (stabilne URL-e). Listingi zawsze force.
DETAIL_TTL = 3 * 86400

BASE = "https://bip.um.walbrzych.pl"
LIST_ROOT = f"{BASE}/interpelacje"
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


def _match_initials(init, surname):
    """'M. Kalinowski' -> jedyny klucz config o tym inicjale+nazwisku."""
    hits = [n for n in _CLUB_ASSIGN
            if n.split()[-1].lower() == surname.lower()
            and n.split()[0][0].lower() == init.lower()]
    if len(hits) == 1:
        return hits[0]
    return ""


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url, *, force=False, ttl=DETAIL_TTL):
    for attempt in range(3):
        try:
            return cached_fetch_text(url, session=session, timeout=40,
                                     delay=0.2, force=force, ttl=ttl)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _total_pages(soup):
    for inp in soup.select("input#index-pageNo"):
        mx = inp.get("max")
        if mx:
            return int(mx)
    return 1


def parse_listing(soup):
    """Zwraca [(slug_id, nr, rok, typ_hint, przedmiot_z_href)].  Za mało — detal daje pełne dane."""
    items = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"/interpelacja/(\d+)/([^/\"']+)", href)
        if not m:
            continue
        pid, slug = m.group(1), m.group(2)
        # slug: "{nr}[z?]-{rr}-{i}-{nazwisko}-{typ}-..."
        tm = re.match(r"(\d+)([a-z]?)-(\d{2})-", slug)
        rok = 2000 + int(tm.group(3)) if tm else 0
        nr = tm.group(1) if tm else pid
        typ = "zapytanie" if "-zapytanie" in slug or "zapytanie" in slug else "interpelacja"
        items.append({"pid": pid, "slug": slug, "nr": nr, "rok": rok, "typ": typ})
    return items


def detail_fields(session, url):
    """Zwraca dict z detalu: typ, przedmiot, radny, data_wplywu, rok, tresc_url, odpowiedz_url."""
    html = fetch_text(session, url)
    out = {"typ": "", "przedmiot": "", "radny_raw": "", "data_wplywu": "", "rok": 0,
           "tresc_url": "", "odpowiedz_url": ""}
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    # tabela Szczegóły: Typ wystąpienia | Nr sprawy | Tożsamość radnego | w sprawie
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    tm = re.search(r"Typ wystąpienia\s+(\w+)", txt)
    if tm:
        out["typ"] = tm.group(1).lower()
    # przedmiot = "w sprawie <...>" (ostatni człon tabeli Szczegóły przed "Załączniki")
    pm = re.search(r"Tożsamość radnego\s+[^w]+?\s+w sprawie\s+(.{3,}?)\s+Załączniki", txt)
    if pm:
        out["przedmiot"] = pm.group(1).strip()
    else:
        pm2 = re.search(r"w sprawie\s+(.{3,}?)\s+Załączniki", txt)
        if pm2:
            out["przedmiot"] = pm2.group(1).strip()
    rm = re.search(r"Tożsamość radnego\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+)*)\s+w sprawie", txt)
    if rm:
        out["radny_raw"] = rm.group(1).strip()
    else:
        rm2 = re.search(r"Tożsamość radnego\s+([A-Z]\.\s*[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)", txt)
        if rm2:
            raw = rm2.group(1).strip()
            out["radny_raw"] = re.sub(r"^([A-Z])\.\s*", r"\1. ", raw)
    # data wytworzenia (Data wytworzenia: DD.MM.RRRR)
    dm = re.search(r"Data wytworzenia:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", txt)
    if dm:
        out["data_wplywu"] = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
        out["rok"] = int(dm.group(3))
    # załączniki
    for a in soup.find_all("a", href=True):
        h = a.get("href", "")
        label = a.get_text(" ", strip=True).lower()
        if "download" not in h:
            continue
        if "odpowied" in label or "odpowiedz" in label:
            if not out["odpowiedz_url"]:
                out["odpowiedz_url"] = h if h.startswith("http") else BASE + h
        elif "interpelacj" in label or "zapytani" in label or label in ("interpelacja", "zapytanie"):
            if not out["tresc_url"]:
                out["tresc_url"] = h if h.startswith("http") else BASE + h
    # typ fallback z treści osadzonej w tytule
    if not out["typ"]:
        hm = re.search(r"\b(?:zapytanie|interpelacja)\b", soup.get_text(" ").lower())
        out["typ"] = hm.group(0) if hm else ""
    return out


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Wałbrzych (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None, help="Ogranicz strony (testy)")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje/zapytania — Wałbrzych (BIP) ===")
    html = fetch_text(session, f"{LIST_ROOT}/1/100", force=True, ttl=0)
    soup = BeautifulSoup(html, "html.parser")
    total_pages = _total_pages(soup)
    pages = min(total_pages, args.max_pages) if args.max_pages else total_pages
    print(f"  stron: {total_pages} (przetwarzam {pages})")

    seen = set()
    items = []
    for page in range(1, pages + 1):
        if page > 1:
            time.sleep(DELAY)
            ph = fetch_text(session, f"{LIST_ROOT}/{page}/100", force=True, ttl=0)
            soup = BeautifulSoup(ph, "html.parser") if ph else None
            if soup is None:
                print(f"  [skip] strona {page}")
                continue
        for it in parse_listing(soup):
            if it["pid"] in seen:
                continue
            seen.add(it["pid"])
            items.append(it)
    print(f"  pozycji w listingach (po dedupe): {len(items)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    final = []
    for it in items:
        url = f"{BASE}/interpelacja/{it['pid']}/{it['slug']}"
        d = detail_fields(session, url)
        typ = d["typ"] or it["typ"]
        # radny: unikalny inicjał+nazwisko -> config; fallback fuzzy
        radny, klub = "", ""
        if d["radny_raw"]:
            m = re.match(r"([A-Z])\.\s*([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)", d["radny_raw"])
            if m:
                matched = _match_initials(m.group(1), m.group(2))
            else:
                matched = _match_nominative(d["radny_raw"])
            radny = matched if matched else d["radny_raw"]
            klub = _club_for(matched) if matched else ""
        rec = {
            "cri": it["nr"],
            "typ": typ,
            "rok": d["rok"] or it["rok"],
            "kadencja": "2024-2029" if (d["rok"] or it["rok"]) >= 2024 else "2018-2024",
            "radny": radny,
            "przedmiot": d["przedmiot"],
            "data_wplywu": d["data_wplywu"],
            "klub": klub,
            "odpowiedz_status": "Udzielono" if d["odpowiedz_url"] else "Nie udzielono",
            "tresc_url": d["tresc_url"],
            "odpowiedz_url": d["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": url,
        }
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        final.append(rec)

    # dedupe po bip_url
    uniq, seen_url = [], set()
    for r in final:
        if r["bip_url"] in seen_url:
            continue
        seen_url.add(r["bip_url"])
        uniq.append(r)
    final = uniq

    interp = sum(1 for r in final if r["typ"] == "interpelacja")
    zap = sum(1 for r in final if r["typ"] == "zapytanie")
    answered = sum(1 for r in final if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in final if not r["radny"])
    no_pred = sum(1 for r in final if not r["przedmiot"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Bez radnego: {no_radny} | Bez przedmiotu: {no_pred} | Razem: {len(final)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
