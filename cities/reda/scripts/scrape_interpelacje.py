#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miejskiej w Redzie (IX kad. 2024-2029).

Źródło: BIP Urzędu Miasta w Redzie (https://bip.reda.pl), zakładka
"Rada Miejska > Interpelacje/Zapytania" (menu 69):

    https://bip.reda.pl/dokumenty/menu/69

Struktura (własny CMS /dokumenty/):
  * Listing kategorii = /dokumenty/menu/69 — foldery wg ROKU złożenia
    ("Interpelacje w roku 2026/2025/2024", ...) z linkiem do folderu /dokumenty/{id}.
  * Folder roku = /dokumenty/{id} — tabela rekordów: <tr> z
      <td><a href="/dokumenty/{rec_id}">{tytuł}</a></td>  <td>{data dodania}</td>
    Tytuł = przedmiot (np. "Interpelacja dotycząca ..."). Data dodania = data
    wpływu (publikacji) rekordu.
  * Detal = /dokumenty/{rec_id} — H1 = tytuł/przedmiot; tabela "Załączniki do
    pobrania" z plikami:
      "Interpelacja {Radny}.pdf"  -> /pobierz/{att_id}  (tresc_url)
      "OR.XXXX.XX.2025 odpowiedź.pdf" -> /pobierz/{att_id} (odpowiedz_url)
    Radnego wyciągamy z nazwy pliku interpelacji ("Interpelacja Radosław Farion.pdf").
    Typ (interpelacja/zapytanie) z tytułu rekordu.

Klub radnego z config.json (club_assignments -> clubs). Kadencja: IX kad.
2024-2029 (rok>=2024). Używamy folderów 2024/2025/2026 (bieżąca kadencja).

Output: rekordy w schemacie Radoskop.
"""

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from http_cache import init_cache  # noqa: E402

BASE = "https://bip.reda.pl"
MENU_URL = BASE + "/dokumenty/menu/69"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5

# rok -> folder id (pobierane z listingu, fallback do tych)
YEAR_FOLDER_FALLBACK = {"2026": 9868, "2025": 8993, "2024": 7831}


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


def fetch_text(session, url, retries=3) -> str:
    for attempt in range(retries):
        try:
            time.sleep(DELAY)
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(4)
                continue
            print(f"  [{resp.status_code}] {url}")
        except requests.RequestException as e:
            print(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def parse_year_folder_ids(html: str) -> dict[str, str]:
    """Z listingu menu/69 (lub /dokumenty/menu/69) wyciąga {rok: folder_id}."""
    out = {}
    # wiersze tabeli: <a href="/dokumenty/{id}">Interpelacje w roku {rok}</a>
    for m in re.finditer(
        r'<a href="/dokumenty/(\d+)"[^>]*>([^<]*[Ii]nterpelacje[^<]*)</a>', html
    ):
        link_id = m.group(1)
        label = _clean(m.group(2))
        rm = re.search(r"20\d\d", label)
        if rm:
            out[rm.group(0)] = link_id
    return out


def parse_year_folder(html: str) -> list[dict]:
    """Rekordy z folderu roku: {title, date, href}."""
    out = []
    seen = set()
    tb = re.findall(r"<table.*?</table>", html, re.S | re.I)
    if not tb:
        return out
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tb[0], re.S | re.I)
    for row in rows:
        link = re.search(r'<a href="(/dokumenty/\d+)"[^>]*>(.*?)</a>', row, re.S)
        if not link:
            continue
        href = link.group(1)
        if href in seen:
            continue
        seen.add(href)
        title = _clean(link.group(2))
        date_m = re.search(r"<td>(\d{4}-\d{2}-\d{2})", row)
        date = date_m.group(1) if date_m else ""
        if not title:
            continue
        out.append({"href": urljoin(BASE, href), "title": title, "date": date})
    return out


def _typ_from_title(title: str) -> str:
    t = title.lower()
    if "zapytani" in t:
        return "zapytanie"
    return "interpelacja"


def _radny_from_attachments(files: list[tuple[str, str]]) -> str:
    """Radny z nazwy pliku 'Interpelacja {Imię Nazwisko}.pdf'."""
    for label, _href in files:
        # e.g. "Interpelacja Radosław Farion.pdf"
        m = re.match(r"[Ii]nterpelacj\w*\s+([^\n\r]+?)(?:\.pdf)?\s*$", label)
        if not m:
            continue
        cand = m.group(1).strip().rstrip(".")
        # pomiń przyrostki typu "dot." na końcu
        cand = re.sub(r"\s*dot\.?$", "", cand).strip()
        # kandydat powinien wyglądać jak nazwa (Imię Nazwisko) — co najmniej 2 słowa
        if len(cand.split()) >= 2 and "," not in cand:
            # usuń wiodące słowa typu 'dotycząca' itp.
            if re.match(r"^(dot|ws|w\s+sprawie)", cand, re.I):
                continue
            return cand
    return ""


def parse_detail(html: str, item: dict) -> dict:
    title = item["title"]
    date = item["date"]
    typ = _typ_from_title(title)

    # załączniki: <a href="/pobierz/{id}">label</a> w tabeli Załączniki
    files = []
    for href, label in re.findall(
        r'<a href="(/pobierz/\d+)"[^>]*>(.*?)</a>', html, re.S
    ):
        lab = _clean(label).rstrip(" .")
        # usuń przyrostek rozmiaru "(PDF, 477.07Kb)"
        lab = re.sub(r"\s*\(\s*PDF\s*,\s*[0-9.,]+\s*(?:Kb|kB|KB|MB)?\s*\)\s*$", "", lab)
        if not lab or "Pobierz" in lab:
            continue
        files.append((lab, urljoin(BASE, href)))

    tresc_url, odpowiedz_url = "", ""
    for lab, href in files:
        low = lab.lower()
        if "odpowiedź" in low or "odpowiedz" in low or low.startswith("or."):
            if not odpowiedz_url:
                odpowiedz_url = href
        elif "interpelacj" in low or "zapytan" in low:
            if not tresc_url:
                tresc_url = href
    if not tresc_url and files:
        tresc_url = files[0][1]

    radny = _radny_from_attachments(files)

    rok = int(date[:4]) if date[:4].isdigit() else 0

    # przedmiot: tytuł rekordu (usunąć wiodący typ)
    przedmiot = title
    przedmiot = re.sub(
        r"^(Interpelacja|Zapytanie)[:\s]+", "", przedmiot, flags=re.I
    ).strip()

    return {
        "cri": item["href"].rstrip("/").split("/")[-1],
        "typ": typ,
        "rok": rok,
        "kadencja": "2024-2029" if rok >= 2024 else ("2018-2024" if rok else ""),
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": date,
        "klub": _club_for(radny),
        "odpowiedz_status": "Udzielono" if odpowiedz_url else "Nie udzielono",
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "data_odpowiedzi": "",
        "bip_url": item["href"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji/zapytań — Reda (BIP bip.reda.pl)"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()

    init_cache(args.cache_dir)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("=== Interpelacje — Reda (BIP bip.reda.pl) ===")
    menu_html = fetch_text(session, MENU_URL)
    year_ids = parse_year_folder_ids(menu_html) if menu_html else {}
    # uzupełnij brakujące roki fallbackiem (jeśli menu już ich nie pokazuje)
    for y, fid in YEAR_FOLDER_FALLBACK.items():
        year_ids.setdefault(y, str(fid))
    print(f"  Foldery lat: {year_ids}")

    records = []
    for year in ["2026", "2025", "2024"]:
        fid = year_ids.get(year)
        if not fid:
            print(f"  brak folderu {year}, pomijam")
            continue
        folder_html = fetch_text(session, urljoin(BASE, f"/dokumenty/{fid}"))
        items = parse_year_folder(folder_html)
        print(f"  {year}: {len(items)} rekordów")
        for it in items:
            # data z rekordu folderu -> rok
            rok = int(it["date"][:4]) if it["date"][:4].isdigit() else 0
            if rok < 2024:
                continue
            detail_html = fetch_text(session, it["href"])
            if not detail_html:
                print(f"  [skip] brak treści: {it['href']}")
                continue
            rec = parse_detail(detail_html, it)
            records.append(rec)

    # dedupe po tresc_url / cri
    seen = set()
    uniq = []
    for r in records:
        k = r["cri"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    records = uniq

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

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
