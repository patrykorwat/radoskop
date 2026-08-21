#!/usr/bin/env python3
"""Generic eSesja interpelacje/zapytania scraper for Radoskop.

eSesja (esesja.pl) "Interpelacje i zapytania" module. Stable URL conventions across
cities, small per-city differences in the detail page, so one parameterised scraper
covers any municipality on the platform.

Listing:  {base}/interpelacje_i_zapytania/{page}
    <div class="user-item"><p class="title"><a href="/interpelacja/{id}_{hash}/{slug}.htm">
        ...</a></p>
        <p class="subtitle"><autorzy>Radny</autorzy> - Interpelacja z dnia {DD miesiąc YYYY}</p>
    Typ/data z subtitle; radny z subtitle (autorzy zbiorowi -> radny="", nie zgadujemy).

Detail (URL utf-8 czytane jako latin-1 -> _fix_url):
    <h1>Rada ... Interpelacje i zapytania</h1>
    <h1>Interpelacja 600/2025 (PRZEDMIOT)</h1>          <- przedmiot w nawiasie (niektóre miasta)
    <h2>Autorzy: Radny , dodano: 19 listopada 2025</h2>
    <div class='wpis'><p>PRZEDMIOT</p></div><div class='iinfo'>...</div>   <- przedmiot (inne miasta)
    <div class='wpis'><p>Załącznik do Interpelacja (f.pdf)</p></div>
        <div class='iinfo'>...<a class='wiecej' href='/interpelacje/{rid}/{id}/{hash}.pdf'>Pobierz</a></div>
    <div class='wpis'><p>Odpowiedź do Interpelacja (f.pdf)</p></div>  (jeśli udzielono)
    tresc = PDF 'Załącznik', odpowiedz = PDF 'Odpowiedź', data z iinfo odpowiedzi.
    Przedmiot: najpierw z wpis (pierwszy, przed 'Załącznik'), fallback do h1 w nawiasie.

Klub radnego z config.json (club_assignments -> clubs, fuzzy do mianownika).
Dedupe po bip_url (id z URL detalu). Rekordy < 2024 odrzucane (--all dla starszych).
"""
from __future__ import annotations

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
sys.path.insert(0, str(HERE.parent / "scripts"))

from http_cache import init_cache  # noqa: E402

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False


class EsesjaInterpScraper:
    def __init__(self, slug: str, base_url: str, councilors: dict[str, str] | None = None):
        self.slug = slug
        self.base = base_url.rstrip("/")
        self.clubs_path = HERE.parent.parent / "cities" / slug / "config.json"
        self.club_assign, self.clubs = self._load_clubs(councilors)

    def _load_clubs(self, councilors):
        assign, clubs = councilors or {}, {}
        if self.clubs_path.is_file():
            try:
                cfg = json.loads(self.clubs_path.read_text(encoding="utf-8"))
                if councilors is None:
                    assign = cfg.get("club_assignments", {}) or {}
                clubs = cfg.get("clubs", {}) or {}
            except Exception:
                pass
        return assign, clubs

    def _club_for(self, radny: str) -> str:
        code = self.club_assign.get(radny, "")
        if not code:
            return ""
        club = self.clubs.get(code)
        return club.get("name", "") if isinstance(club, dict) else ""

    def _match_nominative(self, parsed: str) -> str:
        if not parsed:
            return ""
        best, best_ratio = "", 0.0
        for name in self.club_assign:
            ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, name
        return best if best_ratio >= 0.6 else ""

    @staticmethod
    def _fix_url(url: str) -> str:
        try:
            return url.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return url

    @staticmethod
    def _session() -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def fetch_text(self, session, url: str) -> str:
        url2 = self._fix_url(url)
        for attempt in range(3):
            try:
                resp = session.get(url2, timeout=40)
                if resp.status_code == 200:
                    resp.encoding = "utf-8"
                    return resp.text
                if resp.status_code in (403, 429):
                    time.sleep(3)
                    continue
            except requests.RequestException as e:
                if _DEBUG:
                    print(f"  błąd {url2}: {e}")
                time.sleep(2)
        return ""

    @staticmethod
    def _clean(s) -> str:
        s = re.sub(r"<[^>]+>", " ", s or "")
        s = s.replace("&nbsp;", " ")
        return re.sub(r"\s+", " ", unescape(s)).strip()

    def _last_page(self, soup) -> int:
        best = 1
        for a in soup.select("ul.pager a"):
            m = re.search(r"/interpelacje_i_zapytania/(\d+)$", a.get("href", ""))
            if m:
                best = max(best, int(m.group(1)))
        return best

    def parse_list_items(self, soup):
        items = []
        for item in soup.select("div.user-item"):
            a = item.select_one("p.title a[href*='/interpelacja/']")
            if not a:
                continue
            href = a.get("href")
            sub_el = item.select_one("p.subtitle")
            sub = self._clean(sub_el.get_text(" ", strip=True)) if sub_el else ""
            m = re.search(
                r"^(?P<radny>.+?)\s*-\s*(?P<typ>interpelacj[ae]|zapytani[ae]|wniosk[ie]|zapytani[ae])"
                r"\s+z dnia\s+(?P<dd>\d{1,2})\s+(?P<mon>[a-ząćęłńóśźż]+)\s+(?P<yy>20\d{2})",
                sub, re.I)
            if not m:
                if _DEBUG:
                    print(f"  [parse] brak danych w subtitle: {sub!r}")
                continue
            typ_raw = m.group("typ")
            if typ_raw.lower().startswith("zapytani"):
                typ = "zapytanie"
            elif typ_raw.lower().startswith("wniosk"):
                typ = "wniosek"
            else:
                typ = "interpelacja"
            rok = int(m.group("yy"))
            data_wplywu = f"{rok}-{_MONTHS.get(m.group('mon').lower(), 0):02d}-{int(m.group('dd')):02d}"
            radny_raw = self._clean(m.group("radny"))
            collective = (", " in sub) or (" i " in radny_raw) or ("oraz" in radny_raw.lower())
            if collective:
                radny, klub = "", ""
            else:
                matched = self._match_nominative(radny_raw)
                radny = matched if matched else radny_raw
                klub = self._club_for(matched) if matched else ""
            items.append({
                "href": href, "typ": typ, "radny": radny, "klub": klub,
                "data_wplywu": data_wplywu, "rok": rok,
            })
        return items

    @staticmethod
    def _przedmiot_from_h1(html: str) -> str:
        # "Interpelacja 600/2025 (PRZEDMIOT)" / "Zapytanie ... (PRZEDMIOT)"
        for m in re.finditer(r"<h1[^>]*>(?P<txt>.*?)</h1>", html, re.S):
            txt = re.sub(r"<[^>]+>", " ", m.group("txt"))
            txt = re.sub(r"\s+", " ", unescape(txt)).strip()
            pm = re.search(r"\((?P<p>[^()]{5,})\)$", txt)
            if pm and re.match(r"^(interpelacj|zapytani|wniosk)", txt, re.I):
                return pm.group("p").strip()
        return ""

    def detail(self, session, url: str):
        html = self.fetch_text(session, url)
        if not html:
            return "", "", "", ""
        przedmiot, tresc_url, odpowiedz_url, data_odp = "", "", "", ""
        pairs = []  # (txt, info) in document order
        for m in re.finditer(
                r"<div class=['\"]wpis['\"]><p>(?P<txt>.*?)</p></div>\s*"
                r"<div class=['\"]iinfo['\"]>(?P<info>.*?)</div>", html, re.S):
            txt = self._clean(m.group("txt"))
            info = m.group("info")
            pairs.append((txt, info))
            low_txt = txt.lower().lstrip()
            if low_txt.startswith("odpowiedź") or low_txt.startswith("odpowiedz"):
                hm = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", info)
                if hm and not odpowiedz_url:
                    odpowiedz_url = self._fix_url(hm.group(1))
                    if not odpowiedz_url.startswith("http"):
                        odpowiedz_url = self.base + odpowiedz_url
                    dm = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", info)
                    if dm:
                        mo = _MONTHS.get(dm.group(2).lower())
                        if mo:
                            data_odp = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
            elif low_txt.startswith("załącznik") or low_txt.startswith("zalacznik"):
                hm = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", info)
                if hm and not tresc_url:
                    tresc_url = self._fix_url(hm.group(1))
                    if not tresc_url.startswith("http"):
                        tresc_url = self.base + tresc_url
        # Przedmiot: zwięzła treść z h1 (nawias) w pierwszej kolejności (spójnie
        # z innymi miastami); fallback do pełnego tekstu pierwszego wpisu.
        przedmiot = self._przedmiot_from_h1(html)
        if not przedmiot:
            for ptxt, _pinfo in pairs:
                if ptxt:
                    przedmiot = ptxt
                    break
        # Odpowiedź inline (np. Złotoryja): drugi wpis = odpowiedź tekstowa, bez PDF.
        # Wtedy odpowiedz_url = strona detalu (tam opublikowana odpowiedź).
        if not odpowiedz_url and not tresc_url and len(pairs) >= 2:
            ans_txt, ans_info = pairs[1]
            odpowiedz_url = url
            dm = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})", ans_info)
            if dm:
                mo = _MONTHS.get(dm.group(2).lower())
                if mo:
                    data_odp = f"{dm.group(3)}-{mo:02d}-{int(dm.group(1)):02d}"
            if not przedmiot and ans_txt:
                przedmiot = ans_txt
        return przedmiot, tresc_url, odpowiedz_url, data_odp

    def run(self, output: str, cache_dir=None, all_years=False, max_pages=None) -> int:
        init_cache(cache_dir)
        session = self._session()
        print(f"=== Interpelacje — {self.slug} (eSesja) ===")
        html = self.fetch_text(session, f"{self.base}/interpelacje_i_zapytania")
        soup = BeautifulSoup(html, "html.parser")
        total_pages = self._last_page(soup)
        pages = min(total_pages, max_pages) if max_pages else total_pages
        print(f"  stron listingu: {total_pages} (przetwarzam {pages})")

        items = list(self.parse_list_items(soup))
        for page in range(2, pages + 1):
            time.sleep(DELAY)
            ph = self.fetch_text(session, f"{self.base}/interpelacje_i_zapytania/{page}")
            if not ph:
                print(f"  [skip] strona {page} brak treści")
                continue
            items.extend(self.parse_list_items(BeautifulSoup(ph, "html.parser")))

        seen_url, uniq = set(), []
        for it in items:
            if it["href"] in seen_url:
                continue
            seen_url.add(it["href"])
            uniq.append(it)
        items = uniq
        print(f"  rekordów w listingach (po dedupe): {len(items)}")

        min_rok = None if all_years else 2024
        records = []
        for it in items:
            if min_rok and it["rok"] < min_rok:
                continue
            bip_url = self._fix_url(it["href"] if it["href"].startswith("http") else self.base + it["href"])
            idm = re.search(r"/interpelacja/([^/]+)/", bip_url)
            cri = f"cri-{self.slug}-{idm.group(1)}" if idm else f"cri-{self.slug}-{len(records)}"
            przedmiot, tresc_url, odpowiedz_url, data_odp = self.detail(session, bip_url)
            records.append({
                "cri": cri,
                "typ": it["typ"],
                "rok": it["rok"],
                "kadencja": "2024-2029" if it["rok"] >= 2024 else "2018-2024",
                "radny": it["radny"],
                "przedmiot": przedmiot or it.get("przedmiot", ""),
                "data_wplywu": it["data_wplywu"],
                "klub": it["klub"],
                "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
                "tresc_url": tresc_url,
                "odpowiedz_url": odpowiedz_url,
                "data_odpowiedzi": data_odp,
                "bip_url": bip_url,
            })
            time.sleep(DELAY)

        records.sort(key=lambda r: r["data_wplywu"], reverse=True)
        seen, final = set(), []
        for r in records:
            if r["bip_url"] in seen:
                continue
            seen.add(r["bip_url"])
            final.append(r)
        records = final

        interp = sum(1 for r in records if r["typ"] == "interpelacja")
        zap = sum(1 for r in records if r["typ"] == "zapytanie")
        answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
        no_radny = sum(1 for r in records if not r["radny"])
        print("\n=== Podsumowanie ===")
        print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | "
              f"Bez radnego: {no_radny} | Razem: {len(records)}")

        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
        return 0


def _load_councilors(slug: str) -> dict:
    cfg_path = HERE.parent.parent / "cities" / slug / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        return (json.loads(cfg_path.read_text(encoding="utf-8")) or {}).get("club_assignments", {}) or {}
    except Exception:
        return {}


def make_main(slug: str, base_url: str):
    def _main() -> int:
        global _DEBUG
        parser = argparse.ArgumentParser(description=f"Scraper interpelacji — {slug} (eSesja)")
        parser.add_argument("--output", default=f"cities/{slug}/docs/interpelacje.json")
        parser.add_argument("--cache-dir", default=None)
        parser.add_argument("--all", action="store_true", help="Też starsze kadencje")
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--max-pages", type=int, default=None)
        args = parser.parse_args()
        _DEBUG = args.debug
        scraper = EsesjaInterpScraper(slug, base_url, councilors=_load_councilors(slug))
        return scraper.run(args.output, args.cache_dir, all_years=args.all, max_pages=args.max_pages)
    return _main
