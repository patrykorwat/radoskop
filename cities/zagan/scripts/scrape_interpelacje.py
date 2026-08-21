#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Żaganiu (IX kad. 2024-2029).

Źródło: BIP Żagań (bip.zagan.pl) — kategoria 'Interpelacje radnych wraz z
udzielonymi odpowiedziami' (CMS Perły: /{kat}/{id}/{slug}/, paginacja /{strona}/).
Rejestr realny i osiągalny; każdy wpis = osobny artykuł z załącznikiem PDF
(Interpelacja + Odpowiedź).

UWAGA (source=partial): radny jest dostępny TYLKO jako inicjały w nazwie pliku
PDF (np. 'M.K._-_interp._-_toalety.pdf' / 'P.L._-_odp._na_interp._...') oraz w
treści skanu PDF. Nie mapujemy inicjałów->nazwiska (wieloznaczne: np. M.K. może
być Małgorzata Klorek), więc pole radny = "" (nie zgadujemy). Przedmiot z tytułu
artykułu, data = 'Data wytworzenia informacji' z metadanych, typ (interpelacja/
zapytanie) z tytułu ('zapytanie do Burmistrza' = zapytanie). Odpowiedź = PDF
'Odpowiedź'.

Dedupe po bip_url (id z URL detalu). Tylko bieżąca kadencja (rok>=2024).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""
import argparse, json, re, sys, time
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

KAT = 386
CAT_URL = f"https://bip.zagan.pl/{KAT}/Interpelacje_radnych_wraz_z_udzielonymi_odpowiedziami/"
BASE = "https://bip.zagan.pl"
MIN_ROK_DEFAULT = 2024
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
           "Accept-Language": "pl-PL,pl;q=0.9"}
DELAY = 0.6
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


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
        except requests.RequestException:
            time.sleep(2)
    return ""


def _last_page(soup):
    best = 1
    for a in soup.find_all("a", href=True):
        m = re.search(rf"/{KAT}/Interpelacje_radnych_wraz_z_udzielonymi_odpowiedziami/(\d+)/", a["href"])
        if m:
            best = max(best, int(m.group(1)))
    return best


def parse_list_items(soup):
    items = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"/386/(\d+)/([^/]+)/?$", a["href"])
        if not m:
            continue
        # tylko wpisy kategorii (interpelacje), nie podstrony /kadencja/, /archiwum/ itp
        url = a["href"]
        if not re.search(rf"/386/\d+/.+", url):
            continue
        # page navigation links /386/{page}/... no wait, pagination is /386/.../N/
        if re.search(rf"/386/Interpelacje_radnych[^/]*/\d+/", url):
            continue
        if "/archiwum/" in url:
            continue
        tid = m.group(1)
        title = _clean(a.get_text(" ", strip=True))
        if not title:
            continue
        items.append({"id": tid, "url": url, "title": title})
    return items


def _mk_abs(url):
    return url if url.startswith("http") else (BASE + url if url.startswith("/") else url)


def detail(session, url):
    html = fetch_text(session, url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    inf = soup.find("div", class_="information")
    if inf:
        t = _clean(inf.get_text(" ", strip=True))
        # tytuł = tekst przed 'Informacja ogłoszona dnia' (w .information-parameters)
        cut = re.split(r"\s*Informacja ogłoszona dnia\b", t, maxsplit=1, flags=re.I)
        t = cut[0]
        if t:
            title = t
    if not title:
        h1 = soup.find(["h1", "h2", "h3"])
        if h1 and h1.get_text(" ", strip=True):
            title = _clean(h1.get_text(" ", strip=True))
    # meta: data wytworzenia
    data = ""
    for th in soup.find_all("th"):
        label = _clean(th.get_text(" ", strip=True))
        if label.startswith("Data wytworzenia informacji"):
            td = th.find_next("td")
            if td:
                v = _clean(td.get_text(" ", strip=True))
                m = re.search(r"(\d{4}-\d{2}-\d{2})", v)
                if m:
                    data = m.group(1)
            break
    # PDF attachments
    tresc, odpowiedz = "", ""
    for a in soup.find_all("a", href=True):
        if "pelniacz?" not in a["href"] and "plik=" in a["href"]:
            txt = _clean(a.get_text(" ", strip=True))
            if txt.startswith("Interpelacja") or txt.startswith("Zapytanie"):
                if not tresc:
                    tresc = _mk_abs(a["href"])
            elif txt.startswith("Odpowiedź"):
                if not odpowiedz:
                    odpowiedz = _mk_abs(a["href"])
    # typ z tytułu
    typ = "interpelacja"
    if "zapytanie" in title.lower() or "zapyt." in title.lower():
        typ = "zapytanie"
    rok = int(data[:4]) if len(data) >= 4 and data[:4].isdigit() else 0
    return {"title": title, "typ": typ, "rok": rok, "data_wplywu": data,
            "tresc_url": tresc, "odpowiedz_url": odpowiedz}


def main():
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Żagań (BIP)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()
    print("=== Interpelacje — Żagań (BIP bip.zagan.pl) ===")
    html = fetch_text(session, CAT_URL)
    soup = BeautifulSoup(html, "html.parser")
    total_pages = _last_page(soup)
    pages = min(total_pages, args.max_pages) if args.max_pages else total_pages
    print(f"  stron listingu: {total_pages} (przetwarzam {pages})")

    # page 1
    items = []
    items.extend(parse_list_items(soup))
    for page in range(2, pages + 1):
        time.sleep(DELAY)
        ph = fetch_text(session, f"{CAT_URL}{page}/")
        if not ph:
            print(f"  [skip] strona {page} brak treści")
            continue
        items.extend(parse_list_items(BeautifulSoup(ph, "html.parser")))

    # dedupe
    seen = set()
    uniq = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    items = uniq
    print(f"  wpisów w listingach (po dedupe): {len(items)}")

    min_rok = None if args.all else MIN_ROK_DEFAULT
    records = []
    for it in items:
        det = detail(session, it["url"])
        if not det:
            print(f"  [skip] detal {it['url']} brak treści")
            continue
        if min_rok and det["rok"] and det["rok"] < min_rok:
            continue
        rec = {
            "cri": f"cri-zagan-{it['id']}",
            "typ": det["typ"],
            "rok": det["rok"],
            "kadencja": "2024-2029" if det["rok"] >= 2024 else "2018-2024",
            "radny": "",
            "przedmiot": det["title"] or it["title"],
            "data_wplywu": det["data_wplywu"],
            "klub": "",
            "odpowiedz_status": "Udzielono" if det["odpowiedz_url"] else "Nie udzielono",
            "tresc_url": det["tresc_url"],
            "odpowiedz_url": det["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": it["url"],
        }
        records.append(rec)
        time.sleep(DELAY)

    records.sort(key=lambda r: r["data_wplywu"], reverse=True)
    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie (source=partial: radny tylko w PDF, nie mapowany) ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Razem: {len(records)}")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
