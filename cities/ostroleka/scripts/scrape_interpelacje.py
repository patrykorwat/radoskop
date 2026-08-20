#!/usr/bin/env python3
"""Scraper interpelacji, zapytań i wniosków radnych Rady Miasta Ostrołęki.

Źródło: prawdziwy BIP Urzędu Miasta Ostrołęki —

    https://bip.um.ostroleka.pl/interpelacje/{page}/25

(Uwaga: domena `bip.ostroleka.pl` z config.json jest martwa/zwraca pustą
odpowiedź SPA — prawdziwy rejestr jest na `bip.um.ostroleka.pl`, do którego
prowadzi link "BIP" z www.ostroleka.pl.)

Struktura listingu (plik_jednej_tabeli):
  * Rejestr "Interpelacje, zapytania i wnioski" — pełna tabela HTML,
    paginowana `/interpelacje/{page}/25`, kolumny:
        - "Interpelacja w sprawie" -> <a href="/interpelacja/{id}/{slug}">{subject}</a>
        - "Nr sprawy"     -> np. WPR.0003.16.2026 (cri)
        - "Tożsamość radnego" -> autor (mianownik)
  * Rejestr zawiera interpelacje, zapytania i wnioski (typ per-detal).

Struktura detalu (/interpelacja/{id}/{slug}):
  * "Typ wystąpienia": Interpelacja / Zapytanie / Wniosek
  * "Nr sprawy", "Tożsamość radnego", "w sprawie {subject}"
  * Załączniki: "Interpelacja pdf, N kB" + opcjonalnie
    "Odpowiedź pdf, N kB" (=> odpowiedz_status=Udzielono)
  * Metryki (metryczka): "Wytworzył: {autor}" + "Data wytworzenia: DD.MM.YYYY"
    — pierwsza metryka to data wpływu interpelacji, metryka przy załączniku
    "Odpowiedź" to data odpowiedzi. Przedmiot i treść są w zeskanowanych PDF
    (bez warstwy tekstowej) — `przedmiot` uzupełniamy z pola "w sprawie"
    (tytułu/rejestru), bo jest ono podane jawnie w tabeli.

Radny dopasowywany do config.json club_assignments (fuzzy diacritic). Autorów
spoza config (np. klubu/komisji) zostawiamy z klub="".

Output: format Radoskop {cri, typ, rok, kadencja, radny, przedmiot,
data_wplywu, klub, odpowiedz_status, tresc_url, odpowiedz_url,
data_odpowiedzi, bip_url}.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
"""

import argparse
import difflib
import html as htmllib
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

BIP_BASE = "https://bip.um.ostroleka.pl"
LISTING_URL = f"{BIP_BASE}/interpelacje/{{page}}/25"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.6
MAX_PAGES = 40
PER_PAGE = 25

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


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


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _fuzzy_club_radny(name: str) -> str:
    if name in _CLUB_ASSIGN:
        return name
    best_key, best_score = "", 0.0
    for key in _CLUB_ASSIGN:
        s = difflib.SequenceMatcher(None, name.lower(), key.lower()).ratio()
        if s > best_score:
            best_score, best_key = s, key
    return best_key if best_score >= 0.72 else name


def _normalize_author(raw: str) -> str:
    """Oczyść autora z tytułów/afiliacji (np. 'Wydział PMK')."""
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    # Autor zbiorowy (klub/komisja): zachowaj pełną nazwę, bez przypisywania klubowi.
    if re.match(r"^\s*(Klub|Komisja)", name, re.I):
        return re.sub(r"\s*[,-]\s*$", "", name).strip()
    name = re.sub(r"\s*(Radn[ae]|Radnych|Rady Miasta).*$\s*", "", name, flags=re.I).strip()
    name = name.strip(" ,;:-")
    if "," in name:
        name = name.split(",")[0].strip()
    return name


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


def _norm_date(d_m_y: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", (d_m_y or "").strip())
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_listing(html: str) -> list[dict]:
    """-> [{"href","subject","cri","radny"}]"""
    out = []
    # każdy rekord zaczyna się od th "Interpelacja w sprawie"
    blocks = re.split(r'<th scope="row">Interpelacja w sprawie</th>', html)[1:]
    for b in blocks:
        link = re.search(r'<a\s+href="(https://[^"]+)"[^>]*>(.*?)</a>', b, re.S)
        cri = re.search(r'<th scope="row">Nr sprawy</th>\s*<td[^>]*>([^<]+)</td>', b, re.S)
        rad = re.search(r'<th scope="row">Tożsamość radnego</th>\s*<td[^>]*>([^<]+)</td>', b, re.S)
        rec = {
            "href": link.group(1) if link else "",
            "subject": htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", link.group(2))).strip()) if link else "",
            "cri": htmllib.unescape(re.sub(r"\s+", " ", cri.group(1)).strip()) if cri else "",
            "radny_raw": htmllib.unescape(re.sub(r"\s+", " ", rad.group(1)).strip()) if rad else "",
        }
        if rec["href"]:
            out.append(rec)
    return out


_TYP_RE = re.compile(r"Typ wystąpienia</th>\s*<td[^>]*>\s*([^<]+?)\s*<", re.S)
_ATTACH_RE = re.compile(r'<a[^>]+href="([^"]*attachments/download/\d+)"[^>]*>\s*([^<]+?)\s*</a>', re.S)
_METRYKA_CELL_RE = re.compile(
    r"<th>\s*Data wytworzenia:\s*</th>\s*<td>(.*?)</td>", re.S
)


def _date_from_cell(cell: str) -> str:
    return _norm_date(re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", cell).group(0)) if re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", cell or "") else ""


def parse_detail(html: str, item: dict) -> dict | None:
    if not html:
        return None

    m_typ = re.search(r"Typ wystąpienia</th>\s*<td[^>]*>\s*([^<]+?)\s*<", html, re.S)
    typ_raw = re.sub(r"\s+", " ", m_typ.group(1)).strip() if m_typ else ""
    typ = "interpelacja" if "interpelacj" in typ_raw.lower() else (
        "zapytanie" if re.search(r"zapytan", typ_raw, re.I) else (
            "wniosek" if re.search(r"wniosk", typ_raw, re.I) else ""))
    if not typ:
        typ = "interpelacja"

    cri = item.get("cri") or ""
    cri = re.sub(r"\s+", " ", cri).strip()

    radny = _fuzzy_club_radny(_normalize_author(item.get("radny_raw") or ""))
    przedmiot = item.get("subject") or ""

    # załączniki: Interpelacja + opcjonalnie Odpowiedź
    attach = _ATTACH_RE.findall(html)
    tresc_url, odpowiedz_url = "", ""
    for href, label in attach:
        url = href if href.startswith("http") else BIP_BASE + href
        low = re.sub(r"<[^>]+>", "", label).lower()
        if "odpowied" in low:
            if not odpowiedz_url:
                odpowiedz_url = url
        elif not tresc_url:
            tresc_url = url
    if not tresc_url and attach:
        tresc_url = href if href.startswith("http") else BIP_BASE + href

    # data wpływu -> pierwsza metryka "Data wytworzenia" (interpelacji)
    dates = [_date_from_cell(c) for c in _METRYKA_CELL_RE.findall(html)]
    dates = [d for d in dates if d]
    data_wplywu = dates[0] if dates else ""
    rok = int(data_wplywu[:4]) if data_wplywu else 0
    if not rok:
        ym = re.search(r"\.(\d{4})$", cri)
        if ym:
            rok = int(ym.group(1))

    # data odpowiedzi -> Data wytworzenia w segmencie załącznika "Odpowiedź"
    data_odp = ""
    if odpowiedz_url:
        oi = html.find("Odpowiedź", html.find("Załączniki") if html.find("Załączniki") >= 0 else 0)
        if oi >= 0:
            tail = html[oi: oi + 6000]
            dm = [_date_from_cell(c) for c in _METRYKA_CELL_RE.findall(tail)]
            dm = [d for d in dm if d]
            if dm:
                data_odp = dm[-1]

    return {
        "cri": cri,
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
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji/zapytań/wniosków radnych z BIP Ostrołęki"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--all", action="store_true",
                        help="Scrapuj też starsze kadencje; domyślnie tylko 2024+")
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else 2024

    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje/zapytania/wnioski — BIP Ostrołęki ===")
    seen: dict[str, dict] = {}
    page = 1
    empty_streak = 0
    while page <= args.max_pages:
        html = fetch_text(session, LISTING_URL.format(page=page))
        time.sleep(DELAY)
        rows = parse_listing(html) if html else []
        new = [r for r in rows if r["href"] not in seen]
        _log(f"  strona {page}: {len(rows)} wierszy, nowych {len(new)}")
        if not new:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0
        for r in new:
            seen[r["href"]] = r
        if page % 5 == 0 and not _DEBUG:
            print(f"  listing strona {page}... ({len(seen)} znalezionych)")
        page += 1
    print(f"  Listing: {len(seen)} rekordów")

    records = []
    for item in seen.values():
        detail_html = fetch_text(session, item["href"])
        time.sleep(DELAY)
        if not detail_html:
            print(f"  [skip] brak treści: {item['href']}")
            continue
        rec = parse_detail(detail_html, item)
        if not rec:
            continue
        if min_rok and rec["rok"] and rec["rok"] < min_rok:
            continue
        records.append(rec)

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)
    records.sort(key=lambda r: (r["typ"] != "interpelacja"))

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    wni = sum(1 for r in records if r["typ"] == "wniosek")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Wnioski:       {wni}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
