#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Otwocka (IX kad. 2024-2029).

Źródło: BIP Otwocka (https://bip.otwock.pl), zakładka
"Zapytania i interpelacje Radnych w kadencji 2024-2029" (?bip=1&cid=1502).

Struktura (autorski CMS `?bip=N&cid=..&id=..`):
  * Listing = ?bip=1&cid=1502, artykuły w `<div class="submenu">`:
      `<a href="?bip=2&amp;cid=1502&amp;id={ID}">{tytuł}</a>`
    Typ w `<span class="strona-skrot">` ("Interpelacja" / "Interpelacja Odpowiedź"),
    data w `<div class="strona-data">Data wytworzenia <span>YYYY-MM-DD</span>`.
    Zakładka nie jest paginowana (wszystkie artykuły na jednej stronie).
  * Detal = ?bip=2&cid=1502&id={ID}; H1 "Treść zakładki {tytuł}"; załączniki PDF
    w `<a href="fls/bip_pliki/.../{typ}_*.pdf">{Interpelacja|Zapytanie|Odpowiedź}</a>`.
    "Interpelacja"/"Zapytanie" -> tresc_url, "Odpowiedź" -> odpowiedz_url.

Tytuł koduje autora w dopełniaczu ("Interpelacja radnego Krystiana Kiełtyki",
"Irterpelacja radnych Barbary Dylejko-Menin i Krystiana Kiełtyki"); autorów
zbiorowych ("Interpelacja Klubu Radnych Koalicji Obywatelskiej") zostawiamy bez
radnego (klub="" — nie zgadujemy). Radny dopasowywany do config.json
club_assignments przez fuzzy (surname-stem) dopasowanie dopełniacza do mianownika.

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

LISTING_URL = "https://bip.otwock.pl/?bip=1&cid=1502"
BIP_BASE = "https://bip.otwock.pl"

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
    """Rdzeń nazwiska (do fuzzy): złożenie pierwszych ~6 znaków każdego wyrazu."""
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
    for key, code in _CLUB_ASSIGN.items():
        ks = _stem(key)
        # dopasowanie rdzeni nazwisk (pełna nazwa)
        score = difflib.SequenceMatcher(None, gs, ks).ratio()
        # dopasowanie po samym nazwisku (ostatni wyraz)
        g_last = _stem(g.split()[-1])
        k_last = _stem(key.split()[-1]) if key.split() else ""
        slast = difflib.SequenceMatcher(None, g_last, k_last).ratio()
        s = max(score, slast)
        if s > best:
            best, best_key = s, key
    return best_key if best >= 0.55 else gen


def _radny_from_title(title: str) -> str:
    """Wyciągnij dopełniacz autora z tytułu np. 'Interpelacja radnego Krystiana
    Kiełtyki' / 'radnych Barbary Dylejko-Menin i Krystiana Kiełtyki'.
    Autorzy zbiorowi (Klub Radnych ...) -> ""."""
    t = HH.unescape(title)
    m = re.search(
        r"\bradn(?:ego|ych|a|ej)\s+", t, re.I
    )
    club_m = re.match(r"^.*\bKlub[^,]*\b", t, re.I)
    if club_m:
        return ""
    if not m:
        return ""
    rest = t[m.end():].strip()
    # pierwszy autor (do ' i ' / ' oraz ' / końca)
    author = re.split(r"\s+(?:i|oraz)\s+", rest, maxsplit=1)[0]
    author = re.sub(r"\s*[-–].*$", "", author).strip()
    return _fuzzy_radny(author)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url: str) -> str:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _abs(u: str) -> str:
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return BIP_BASE + ("/" if not u.startswith("/") else "") + u


def parse_listing(html: str) -> list[dict]:
    """Parseuj listing przez podział na bloki artykułów (`div.submenu`)."""
    out = []
    blocks = html.split('<div class="submenu">')[1:]
    for b in blocks:
        mm = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not mm:
            continue
        href = mm.group(1)
        # artykuł ma ?bip=2&cid=1502&id={ID} — weź OSTATNI numer (cid nadpisuje id)
        ids = re.findall(r"id=(\d+)", href)
        if not ids:
            continue
        idm = ids[-1]
        title = re.sub(r"\s+", " ", HH.unescape(re.sub(r"<[^>]+>", "", mm.group(2)))).strip()
        sm = re.search(
            r'class="strona-skrot[^"]*"[^>]*>(.*?)</div>', b, re.S
        )
        skrot = ' '.join(HH.unescape(sm.group(1)).split()).strip() if sm else ""
        dm = re.search(r"Data wytworzenia\s*<span>([^<]*)</span>", b)
        date = dm.group(1).strip() if dm else ""
        out.append(
            {
                "id": idm,
                "href": _abs(href.replace("&amp;", "&")),
                "title": title,
                "skrot": skrot,
                "date": date,
            }
        )
    return out


_ATTACH_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']*\.(?:pdf|PDF|doc|docx)[^"\']*)["\'][^>]*>(.*?)</a>',
    re.S,
)


def parse_detail(html: str, item: dict) -> dict:
    title = item["title"]
    # typ: dominujący w tytule / skrot
    typ_raw = (item["skrot"] + " " + title).lower()
    typ = "interpelacja" if "interpelacj" in typ_raw else (
        "zapytanie" if "zapytan" in typ_raw else "wniosek"
    )

    radny = _radny_from_title(title)

    # przedmiot: segment po 'w sprawie'
    pm = re.search(r"\bw sprawie\b\s+(.*)$", title, re.I)
    przedmiot = re.sub(r"\s+", " ", pm.group(1)).strip() if pm else ""

    # załączniki
    tresc_url, odpowiedz_url = "", ""
    files = []
    for href, label in _ATTACH_RE.findall(html):
        lab = ' '.join(HH.unescape(re.sub(r"<[^>]+>", " ", label)).split())
        files.append((lab, _abs(href.replace("&amp;", "&"))))
    for lab, href in files:
        low = lab.lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = href
        elif "interpelacj" in low or "zapytan" in low or "wnios" in low or "tresc" in low:
            if not tresc_url:
                tresc_url = href
    if not tresc_url and files:
        tresc_url = files[0][1]

    # data z detalu (autorytatywna), fallback do listingu
    date = item["date"]
    dm = re.search(r"Data wytworzenia\s*(?:<span>)?([\d-]{10})", html)
    if dm and dm.group(1).strip():
        date = dm.group(1).strip()

    rok = int(date[:4]) if date[:4].isdigit() else 0

    return {
        "cri": item["id"],
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else "2018-2024",
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": date,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": item["href"],
    }


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji/zapytań radnych z BIP Otwocka (cid 1502)"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Otwocka (cid 1502) ===")
    listing_html = fetch_text(session, LISTING_URL)
    items = {r["id"]: r for r in parse_listing(listing_html)}
    print(f"  Listing: {len(items)} rekordów")

    records = []
    for item in items.values():
        detail_html = fetch_text(session, item["href"])
        if not detail_html:
            print(f"  [skip] brak treści: {item['href']}")
            continue
        rec = parse_detail(detail_html, item)
        if rec["rok"] and rec["rok"] < 2024:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    records.sort(key=lambda r: (r["typ"] != "interpelacja"))

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in records if not r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Bez radnego (zbiorowe/kluby): {no_radny}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\nZapisano {len(records)} rekordów do {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
