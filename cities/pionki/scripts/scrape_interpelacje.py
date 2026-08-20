#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Pionkach (IX kad. 2024-2029).

Źródło: BIP Pionek (https://bip.pionki.pl), sekcja "Interpelacje Radnych Rady
Miasta" — roczne podstrony:
  * 2024 -> /strona-4897-interpelacje_radnych_rok_2024.html
  * 2025 -> /strona-5657-interpelacje_radnych_rok_2025.html
  * 2026 -> /strona-6257-interpelacje_radnych_rok_2026.html
    (2026 = miesiąc podstrony -> detail interpelacji; 2024/2025 = załączniki
    bezpośrednio na stronie roku)

Struktura (port CMS, `powiazane_pliki` / `zalacznik_embeded`):
  * Typ + autor + data + przedmiot w NAZWIE załącznika:
        "Interpelacja Radnej {Autor} z dnia DD.MM.YYYY r. w sprawie {przedmiot}."
        "Odpowiedź na interpelacje {Autor} z dnia DD.MM.YYYY"
  * href="/Common/pobierzPlik/id/{id}/module_short/port/obj_id/{obj}/...html"
  * Dla 2026: rok page -> miesiąc subpage -> detail interpelacji (każda ma
    swój załącznik PDF, odpowiedzi jako osobne strony jeśli występują).

Radny dopasowywany do config.json club_assignments przez fuzzy (surname-stem)
dopasowanie dopełniacza do mianownika; autorzy zbiorowi bez radnego -> klub="".

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

BIP_BASE = "https://bip.pionki.pl"
# rok -> (year_page_path, tryb: "attach"|"months")
YEARS = {
    2024: "/strona-4897-interpelacje_radnych_rok_2024.html",
    2025: "/strona-5657-interpelacje_radnych_rok_2025.html",
    2026: "/strona-6257-interpelacje_radnych_rok_2026.html",
}
# miesiące (linki podstron) do szukania na stronie roku 2026
MONTH_RE = re.compile(
    r'href="(/strona-\d+-(?:styczen|luty|marzec|kwiecien|maj|czerwiec|lipiec|'
    r'sierpien|wrzesien|pazdziernik|listopad|grudzien)[^"]*)"', re.I
)

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


# Załączniki (2024/2025 bezpośrednio, 2026 na detailu)
_ATTACH_RE = re.compile(
    r'<a class="zalacznik_embeded"[^>]*title="([^"]+)"[^>]*href="(/Common/pobierzPlik/[^"]+)"',
    re.S,
)
_DATE_RE = re.compile(r"z (?:dnia|dn\.?)\s*(\d{1,2})[.\s]+(\d{1,2})[.\s]+(\d{4})", re.I)
_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrze\u015bnia": 9,
    "października": 10, "pa\u017adziernika": 10, "listopada": 11, "grudnia": 12,
}
_DATE_MONTHNAME_RE = re.compile(
    r"z (?:dnia|dn\.?)\s*(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|"
    r"lipca|sierpnia|września|wrze\u015bnia|października|pa\u017adziernika|listopada|grudnia)"
    r"\s+(\d{4})", re.I
)


def _parse_date(label: str):
    m = _DATE_RE.search(label)
    if m:
        d, mo, y = m.groups()
        return "%s-%02d-%02d" % (y, int(mo), int(d))
    m = _DATE_MONTHNAME_RE.search(label)
    if m:
        d, mo_name, y = m.groups()
        mo = _MONTHS.get(mo_name.lower(), 1)
        return "%s-%02d-%02d" % (y, int(mo), int(d))
    return ""


def _canonical_radny(author: str):
    author = re.sub(r"\s+", " ", (author or "")).strip()
    if not author:
        return ""
    if re.search(r"\b[Kk]lub\b", author):
        return ""
    return _fuzzy_radny(author)


def _typ_from_label(label: str) -> str:
    low = label.lower()
    if "zapytanie" in low:
        return "zapytanie"
    return "interpelacja"


def _parse_attach_page(html: str, rok: int, year_url: str) -> list[dict]:
    """2024/2025: pary załączników (Interpelacja/Zapytanie + Odpowiedź) na stronie roku.

    Klucz rekordu = tresc_url (adres załącznika), więc dwa różne wystąpienia tego
    samego radnego tego samego dnia NIE kolidują. Odpowiedź dopasowujemy po
    (autor, data) do rekordu bez jeszcze odpowiedzi.
    """
    blocks = _ATTACH_RE.findall(html)
    # najpierw zbierz główne (interpelacja/zapytanie)
    main: list[dict] = []
    for label, href in blocks:
        label = HH.unescape(label)
        low = label.lower()
        if "odpowied" in low:
            continue
        if "uzupełnienie" in low or "uzupe\u0142nienie" in low:
            continue  # uzupełnienie do interpelacji — pomijamy jako osobny rekord
        url = BIP_BASE + href
        typ = _typ_from_label(label)
        date = _parse_date(label)
        am = re.search(
            r"\b(?:Interpelacja|Zapytanie)[^z]*?\b(Radnej|Radnego|r\.|R\.)"
            r"\s+([\wŁ-]+(?:\s+[\wŁ-]+)*?)\s+(?:z dnia|z dn\.|z\s)", label, re.I
        )
        author = _canonical_radny(am.group(2)) if am else ""
        pm = re.search(r"\bw sprawie:?\s+(.*?)(?:,\s+plik\s+PDF|\.\s*$|$)", label, re.I | re.S)
        przedmiot = pm.group(1).strip() if pm else ""
        rok_rec = int(date[:4]) if date else rok
        if rok_rec < 2024:
            continue
        main.append({
            "cri": "",
            "typ": typ,
            "rok": rok_rec,
            "kadencja": "2024-2029" if rok_rec >= 2024 else "2018-2024",
            "radny": author,
            "przedmiot": przedmiot,
            "data_wplywu": date,
            "klub": _club_for_radny(author),
            "odpowiedz_status": "Nie udzielono",
            "tresc_url": url,
            "odpowiedz_url": "",
            "data_odpowiedzi": "",
            "bip_url": BIP_BASE + year_url,
        })
    # dopasuj odpowiedzi (po autorze+data lub sam daty) do rekordów bez odpowiedzi
    answers = [(HH.unescape(l), BIP_BASE + h) for l, h in blocks if "odpowied" in HH.unescape(l).lower()]
    for alabel, ahref in answers:
        adate = _parse_date(alabel)
        am = re.search(
            r"na\s+interpelacj(?:ę|e)[^.]*?\b(Radnej|Radnego|r\.)\s+([\wŁ-]+(?:\s+[\wŁ-]+)*?)"
            r"\s+(?:z dnia|z dn\.|z\s)", alabel, re.I
        )
        aauthor = _canonical_radny(am.group(2)) if am else ""
        # znajdź pierwszy rekord bez odpowiedzi pasujący po dacie (i jeśli możliwe autorze)
        for rec in main:
            if rec["odpowiedz_url"]:
                continue
            if adate and rec["data_wplywu"] == adate and (not aauthor or rec["radny"] == aauthor):
                rec["odpowiedz_url"] = ahref
                rec["odpowiedz_status"] = "Udzielono"
                rec["data_odpowiedzi"] = adate
                break
    # dedupe po tresc_url
    seen = set()
    out = []
    for rec in main:
        if rec["tresc_url"] in seen:
            continue
        seen.add(rec["tresc_url"])
        out.append(rec)
    out.sort(key=lambda r: (r["data_wplywu"] or "", r["radny"], r["tresc_url"]))
    return out


def _match_answer(records: dict, answer_label: str) -> str | None:
    """Znajdź klucz rekordu pasujący do odpowiedzi (autor+data)."""
    date = _parse_date(answer_label)
    # autor z odpowiedzi
    am = re.search(
        r"na\s+interpelacj(?:ę|e)[^.]*?\b(Radnej|Radnego|r\.)\s+([\wŁ-]+(?:\s+[\wŁ-]+)*?)"
        r"\s+(?:z dnia|z dn\.|z\s)", answer_label, re.I
    )
    author = _canonical_radny(am.group(2)) if am else ""
    for key, rec in records.items():
        if rec["odpowiedz_url"]:
            continue
        if date and rec["data_wplywu"] == date:
            return key
        if author and rec["radny"] == author and not rec["tresc_url"]:
            return key
    return None


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji/zapytań Pionki")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje — BIP Pionki ===")
    records: dict = {}
    for rok in sorted(YEARS):
        year_url = YEARS[rok]
        html = fetch_text(session, BIP_BASE + year_url)
        if not html:
            print("  [skip] brak treści roku %d" % rok)
            continue
        recs = _parse_attach_page(html, rok, year_url)
        for r in recs:
            key = (r["data_wplywu"], r["radny"], r["tresc_url"])
            records[key] = r
        print("  %d: %d rekordów" % (rok, len(recs)))

    # --- 2026: miesiąc podstrony -> detail interpelacji ---
    if args.all or 2026 in YEARS:
        year_url = YEARS.get(2026)
        if year_url:
            html = fetch_text(session, BIP_BASE + year_url)
            if html:
                month_hrefs = set(MONTH_RE.findall(html))
                _log("  miesiące 2026: %d" % len(month_hrefs))
                for mhref in sorted(month_hrefs):
                    mhtml = fetch_text(session, BIP_BASE + mhref)
                    if not mhtml:
                        continue
                    # detail interpelacji na stronie miesiąca
                    detail_hrefs = set(
                        re.findall(
                            r'href="(/strona-\d+-interpelac[^"]*)"', mhtml
                        )
                    )
                    for dhref in detail_hrefs:
                        if "rada_miasta" in dhref or "rok_20" in dhref:
                            continue
                        dhtml = fetch_text(session, BIP_BASE + dhref)
                        if not dhtml:
                            continue
                        for r in _parse_2026_detail(dhtml, BIP_BASE + dhref):
                            key = (r["data_wplywu"], r["radny"], r["tresc_url"])
                            records[key] = r

    # posortuj
    final = sorted(records.values(), key=lambda r: (r["data_wplywu"] or "", r["radny"]))
    interp = sum(1 for r in final if r["typ"] == "interpelacja")
    zap = sum(1 for r in final if r["typ"] == "zapytanie")
    answered = sum(1 for r in final if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print("Interpelacje:  %d" % interp)
    print("Zapytania:     %d" % zap)
    print("Z odpowiedzią: %d" % answered)
    print("Razem:         %d" % len(final))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Zapisano: %s (%.1f KB)" % (out, out.stat().st_size / 1024.0))
    return 0


def _parse_2026_detail(html: str, bip_url: str) -> list[dict]:
    """2026: detail interpelacji — tytuł (h1/tytuł strony) + załącznik PDF.

    Przedmiot/data/autor z <title> lub nagłówka. Odpowiedzi osobne strony (jeśli
    w tytule 'Odpowiedź na...' łączymy z wcześniej znalezionym, ale zwykle detal
    to sama interpelacja; odpowiedzi mogą nie być na stronie)."""
    rec = {
        "cri": "",
        "typ": "interpelacja",
        "rok": 2026,
        "kadencja": "2024-2029",
        "radny": "",
        "przedmiot": "",
        "data_wplywu": "",
        "klub": "",
        "odpowiedz_status": "Nie udzielono",
        "tresc_url": "",
        "odpowiedz_url": "",
        "data_odpowiedzi": "",
        "bip_url": bip_url,
    }
    # tytuł z <title>
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    title = HH.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tm.group(1)))).strip() if tm else ""
    title = re.split(r"\s*[-|]\s*(?:Portal|Biuletyn)", title)[0].strip()
    m = re.search(r"\b(?:Interpelacja|Zapytanie)\b\s+(?:radn(?:ego|ej))?\s*([\wŁ.]+(?:\s+[\wŁ.]+)*?)\s+(?:z dnia|z dn\.|z\s)", title, re.I)
    if m:
        rec["radny"] = _canonical_radny(m.group(1))
        rec["klub"] = _club_for_radny(rec["radny"])
    rec["data_wplywu"] = _parse_date(title)
    pm = re.search(r"\bw spr\.[:.]?\s+(.*)$", title, re.I)
    if not pm:
        pm = re.search(r"\bw sprawie\b\s+(.*)$", title, re.I)
    if pm:
        rec["przedmiot"] = pm.group(1).strip().rstrip(".")
    # załącznik PDF
    att = _ATTACH_RE.findall(html)
    for label, href in att:
        label = HH.unescape(label)
        if "odpowied" in label.lower():
            if not rec["odpowiedz_url"]:
                rec["odpowiedz_url"] = BIP_BASE + href
                rec["odpowiedz_status"] = "Udzielono"
                rec["data_odpowiedzi"] = _parse_date(label)
        elif not rec["tresc_url"]:
            rec["tresc_url"] = BIP_BASE + href
    if not rec["data_wplywu"]:
        return []
    if rec["rok"] and rec["rok"] < 2024:
        return []
    return [rec]


if __name__ == "__main__":
    raise SystemExit(main())
