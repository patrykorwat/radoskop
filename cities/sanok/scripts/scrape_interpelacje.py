#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Sanoka (IX kad. 2024-2029).

Źródło: BIP Sanoka na platformie biuletyn.net (https://sanok.biuletyn.net),
zakładka "Rada Miasta > Rada Miasta IX kadencji > Interpelacje i odpowiedzi na
interpelacje Radnych Rady Miasta" (cid 383) — w podkategoriach PER RADNY
(Drwięga Maciej, Nogaj Grzegorz, ...).

Struktura (biuletyn.net — ten sam rodzinny CMS co Otwock/Radymno):
  * Kategoria IX kad. ma podkategorie per radny: {radny} -> ?bip=1&cid={cat}.
  * Strona radnego: <h2 ...>Treść zakładki {Radny Nazwisko}</h2> i seria
    <p><a href="fls/bip_pliki/...pdf">{opis}</a></p> — na przemian
    "interpelacja ..." i "odpowiedź na interpelację ..." o tym samym przedmiocie.
    (Zdecydowana większość to interpelacje; rzadkie "zapytanie" rozpoznajemy z
    opisu/nazwy pliku.)
  * Data wpływu: z NAZWY pliku aktu, np. "…_23.09.2025.pdf",
    "…z_dn.15.05.2024.pdf", "…27.03.2025…pdf"; gdy jej brak — z folderu
    miesiąca (fls/bip_pliki/YYYY_MM/…).
  * Radny = tytuł podkategorii, dopasowany fuzzy do config.json club_assignments
    (nazwisko+imię -> Imię Nazwisko).
  * odpowiedz_status: "Udzielono" gdy w parze jest plik odpowiedzi.

Site Radoskop Sanok jest "disabled" (głosowania archiwalne/eSesja), ale
interpelacje IX kad. w BIP NIE zależą od eSesji — scraping jest niezależny.

Output: rekordy w schemacie Radoskop.
"""

import argparse
import difflib
import json
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

BASE = "https://sanok.biuletyn.net"
# IX kadencja: kategoria Interpelacje (383) i podkategorie per radny (cids).
CATEGORY_IX = 383
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5

MONTHS_PL_NUM = {
    1: "01", 2: "02", 3: "03", 4: "04", 5: "05", 6: "06",
    7: "07", 8: "08", 9: "09", 10: "10", 11: "11", 12: "12",
}


def _load_clubs() -> tuple[dict, dict]:
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _stem(name: str) -> str:
    name = re.sub(r"[^a-ząćęłńóśźż\s]", "", (name or "").lower()).strip()
    return "".join(w[:6] for w in name.split()) if name else ""


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-ząćęłńóśźż]{2,}", (s or "").lower())


def _fuzzy_radny(gen: str) -> str:
    """'Nazwisko Imię' (tytuł podkategorii) -> kanoniczne 'Imię Nazwisko' z config.

    Autorzy zbiorowi/niejednoznaczni ("grupa Radnych", "Lewandowski Dawid i
    Domaradzki Jerzy") zostawiamy jako "" — nie zgadujemy radnego.
    """
    g = re.sub(r"\s+", " ", (gen or "").strip())
    if not g:
        return ""
    if g in _CLUB_ASSIGN:
        return g
    low = g.lower()
    # autor zbiorowy / grupa / kilku radnych -> bez pojedynczego radnego
    if (" i " in g) or ("grupa" in low) or ("oraz" in low) or ("klub" in low):
        return ""
    gwords = _norm_words(g)
    gset = set(gwords)
    # nazwisko = najdłuższy token (zwykle ostatni w 'Nazwisko Imię')
    g_surname = max(gwords, key=len) if gwords else ""
    best_key, best = "", 0.0
    for key in _CLUB_ASSIGN:
        kset = set(_norm_words(key))
        # dopasowanie po wspólnym nazwisku (token >=4 znaków)
        common = gset & kset
        surname_hit = (len(g_surname) >= 4 and g_surname in kset)
        if common:
            score = 0.8 if surname_hit else 0.6
        else:
            continue
        # imię nie może przeczyć: wspólne imiona powinny się zgadzać
        g_first = [w for w in gwords if w != g_surname]
        k_non_surname = [w for w in kset if w != g_surname]
        name_conflict = g_first and k_non_surname and not (set(g_first) & set(k_non_surname))
        if name_conflict:
            score = 0.55
        if score > best:
            best, best_key = score, key
    return best_key if best >= 0.55 else g


def fetch_text(session, url) -> str:
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(4)
                continue
        except requests.RequestException as e:
            print(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE + ("/" if not href.startswith("/") else "") + href


def _date_from_filename(name: str, folder: str) -> str:
    """Wyciągnij datę z nazwy pliku / folderu. Zwraca RRRR-MM-DD lub ''."""
    n = name or ""
    # DD.MM.RRRR / DD_MM_RRRR / DD-MM-RRRR
    m = re.search(r"(\d{1,2})[._-](\d{1,2})[._-](20\d{2})", n)
    if m:
        d, mo, y = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    # słownie: "27 lipca 2025" / "15 maja 2024"
    m = re.search(
        r"(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|"
        r"września|października|listopada|grudnia)\s+(20\d{2})", n, re.I)
    if m:
        d, mo_w, y = m.groups()
        mo = {
            "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
            "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
            "października": 10, "listopada": 11, "grudnia": 12,
        }[mo_w.lower()]
        return f"{y}-{mo:02d}-{int(d):02d}"
    # folder: fls/bip_pliki/YYYY_MM/...
    m = re.search(r"/(20\d{2})_(\d{2})/", folder)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    return ""


def parse_radny_page(html: str, radny_fallback: str) -> tuple[list[dict], str]:
    """Zwraca (surowe rekordy, radny). Rekordy ze par interpelacja/odpowiedź."""
    # radny z H2 'Treść zakładki {Nazwisko Imię}'
    hm = re.search(r"Treść zakładki</span>\s*([^<]+)</h2>", html, re.S)
    radny = radny_fallback
    if hm:
        radny = _fuzzy_radny(_clean(hm.group(1)))

    # wszystkie <p> z plikiem PDF we fls/bip_pliki (najnowsze na górze)
    blocks = re.findall(r'<p[^>]*>(.*?)</p>', html, re.S)
    items = []  # (label, absurl, folder)
    for b in blocks:
        href = re.search(r'href="([^"]*fls/bip_pliki/[^"]*\.pdf)"', b)
        if not href:
            continue
        am = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if am:
            folder = am.group(1)
            label = _clean(am.group(2))
        else:
            folder = href.group(1)
            label = _clean(b)
        items.append((label, _abs(href.group(1)), folder))

    # Na stronie radnego wpisy idą parami: [interpelacja, odpowiedź na interpelację, ...]
    # (najnowsze na górze). Uwzględnij też wypadki, gdzie odpowiedź jest samodzielna
    # lub następuje po niej (kolejność jak w źródle -> zachowaj kolejność).
    records = []
    last_tresc = None  # indeks ostatniego rekordu bez odpowiedzi
    for label, absurl, folder in items:
        low = label.lower()
        is_odp = ("odpowiedź" in low) or low.startswith("odp_") or low.startswith("odp.")
        date = _date_from_filename(label, folder)
        rok = int(date[:4]) if date[:4].isdigit() else 0

        if is_odp:
            # dopasuj do ostatniego rekordu-treści
            if last_tresc is not None and not records[last_tresc]["odpowiedz_url"]:
                records[last_tresc]["odpowiedz_url"] = absurl
                records[last_tresc]["odpowiedz_status"] = "Udzielono"
                # data odpowiedzi = data pliku odpowiedzi (lub treści)
                if date:
                    records[last_tresc]["data_odpowiedzi"] = date
                last_tresc = None
            else:
                # samodzielna odpowiedź
                records.append({
                    "cri": f"sanok-{rok}-{abs(hash(label)) % 100000}",
                    "typ": "zapytanie" if "zapytan" in low else "interpelacja",
                    "rok": rok,
                    "kadencja": "2024-2029" if rok >= 2024 else ("2018-2024" if rok else ""),
                    "radny": radny,
                    "przedmiot": re.sub(r"^(odpowiedź na interpelację|odpowiedź)\b", "", label, flags=re.I).strip(),
                    "data_wplywu": "",
                    "klub": _club_for(radny),
                    "odpowiedz_status": "Udzielono",
                    "tresc_url": "",
                    "odpowiedz_url": absurl,
                    "data_odpowiedzi": date,
                    "bip_url": "",
                })
                last_tresc = len(records) - 1
        else:
            przedmiot = re.sub(
                r"^(interpelacja|zapytanie)\b", "", label, flags=re.I).strip()
            przedmiot = re.sub(
                r"^(radnego|radnej|radni|w sprawie|ws\.?\s|dot\.\s|dot\.)", "",
                przedmiot, flags=re.I).strip()
            records.append({
                "cri": f"sanok-{rok}-{abs(hash(label)) % 100000}",
                "typ": "zapytanie" if "zapytan" in low else "interpelacja",
                "rok": rok,
                "kadencja": "2024-2029" if rok >= 2024 else ("2018-2024" if rok else ""),
                "radny": radny,
                "przedmiot": przedmiot,
                "data_wplywu": date,
                "klub": _club_for(radny),
                "odpowiedz_status": "Nie udzielono",
                "tresc_url": absurl,
                "odpowiedz_url": "",
                "data_odpowiedzi": "",
                "bip_url": "",
            })
            last_tresc = len(records) - 1
    return records, radny


# Kategorie per radny (IX kad.) — podkategorie kategorii 383. Stała lista
# (ustalona z menu; radni mogą mieć więcej stron — flaga --max-pages ograniczy).
RADNY_CIDS = [
    386, 404, 406, 410, 417, 418, 419, 420, 423, 430,
    431, 432, 470, 474, 479, 493, 519, 522,
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji/zapytań — Sanok (biuletyn.net IX kad.)"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Sanok (biuletyn.net IX kad.) ===")
    records = []
    for cid in RADNY_CIDS:
        url = f"{BASE}/?bip=1&cid={cid}&bsc=N"
        html = fetch_text(session, url)
        if not html:
            print(f"  [skip] brak treści cid {cid}")
            continue
        recs, radny = parse_radny_page(html, "")
        print(f"  cid {cid} ({radny or '?'}): {len(recs)} rekordów")
        records.extend(recs)

    # dedupe po (radny, przedmiot, data)
    seen = set()
    uniq = []
    for r in records:
        k = (r["radny"], r["przedmiot"], r["data_wplywu"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    records = uniq
    records.sort(key=lambda r: (r["data_wplywu"] or "", r["radny"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in records if not r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Bez radnego:   {no_radny}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nZapisano {len(records)} rekordów do {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
