#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Gliwice z BIP Gliwice.

Źródło: https://bip.gliwice.eu/radaMiasta/interpelacje

BIP Gliwice publikuje interpelacje/zapytania w jednej, dużej tabeli (#interpelacje)
na stronie rejestru /radaMiasta/interpelacje. Nagłówek / szczegóły HTML (karta
/radaMiasta/interpelacja/{id}) NIE zawiera nazwiska radnego ani daty złożenia —
te dane są tylko w załączniku PDF (storage/interpelacje/p{id}.pdf), który jest
częściowo tekstowy, a częściowo skanem (pusty tekst). Dla skanów radny/data nie
są dostępne bez OCR — pola wtedy zostają puste (uczciwie, bez zgadywania).

Pobierane z HTML (pewnie):
  cri, typ (interpelacja/zapytanie), przedmiot, tresc_url, odpowiedz_url, bip_url,
  odpowiedz_status (pdf odpowiedzi obecny ? 'Udzielono' : 'Nie udzielono').

Pobierane z PDF p{id}.pdf (best-effort, gdy tekstowy):
  radny (dopasowany do listy radnych z dropdownu rejestru), data_wplywu, rok.

klub = '' (config Gliwic nie ma club_assignments / clubs).

Użycie:
  python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir cache/
  python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir cache/ --all
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache, cached_fetch_text  # noqa: E402

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

BASE = "https://bip.gliwice.eu"
LIST_URL = f"{BASE}/radaMiasta/interpelacje"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.35
MIN_ROK_DEFAULT = 2024  # IX kadencja (2024-2029)

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


# ---------------------------------------------------------------------------
# Config (kluby — Gliwice nie ma club_assignments)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_text(url: str) -> str:
    return cached_fetch_text(
        url, session=None, headers=HEADERS, timeout=40, delay=DELAY
    )


def _pdf_cache_path(cache_dir: Path, name: str) -> Path:
    d = cache_dir / "pdfs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.pdf"


def fetch_pdf(cache_dir: Path, url: str) -> tuple[Path, str]:
    """Pobiera PDF (cache w cache_dir/pdfs/), zwraca (path, tekst pypdf)."""
    m = re.search(r"interpelacje/([po]\d+)\.pdf", url)
    name = m.group(1) if m else Path(url).stem
    p = _pdf_cache_path(cache_dir, name)
    if not p.exists() or p.stat().st_size < 100:
        time.sleep(DELAY)
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 200 and len(r.content) > 100:
                p.write_bytes(r.content)
        except requests.RequestException as e:
            _log(f"  pdf błąd {url}: {e}")
            return p, ""
    return p, pdf_text(p)


def pdf_text(p: Path) -> str:
    if PdfReader is None or not p.exists():
        return ""
    try:
        r = PdfReader(str(p))
        return "\n".join((pg.extract_text() or "") for pg in r.pages)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Parsing listing
# ---------------------------------------------------------------------------

def parse_listing(html: str) -> tuple[list[dict], list[str]]:
    """Zwraca (karty, radni_z_dropdownu)."""
    cards: list[dict] = []
    radni: list[str] = []
    if BeautifulSoup is None:
        return cards, radni
    soup = BeautifulSoup(html, "html.parser")

    # Radni z dropdownu filtra rejestru (pewna lista radnych kadencji).
    sel = soup.find("select", {"name": "radny"})
    if sel:
        for o in sel.find_all("option"):
            name = o.get_text(" ", strip=True)
            if name and name.lower() != "wszyscy...":
                radni.append(name)

    table = soup.find("table", {"id": "interpelacje"})
    if not table:
        return cards, radni

    for tr in table.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/radaMiasta/interpelacja/(\d+)"))
        if not a:
            continue
        m = re.search(r"interpelacja/(\d+)", a["href"])
        if not m:
            continue
        cri = m.group(1)
        bip = a["href"] if a["href"].startswith("http") else BASE + a["href"]

        txt = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        tm = re.match(r"^(Interpelacja|Zapytanie)\b", txt)
        typ = "interpelacja" if tm and tm.group(1) == "Interpelacja" else "zapytanie"
        pm = re.search(r"Dotyczy:\s*(.*?)(?:Pobierz plik|Tekst|$)", txt)
        przedmiot = pm.group(1).strip() if pm else ""

        tresc_url, odp_url = "", ""
        for pa in tr.find_all("a", href=True):
            mm = re.match(r".*interpelacje/([po])(\d+)\.pdf", pa["href"])
            if mm:
                u = pa["href"] if pa["href"].startswith("http") else BASE + pa["href"]
                if mm.group(1) == "p":
                    tresc_url = u
                else:
                    odp_url = u

        cards.append({
            "cri": cri,
            "typ": typ,
            "przedmiot": przedmiot,
            "tresc_url": tresc_url,
            "odpowiedz_url": odp_url,
            "bip_url": bip,
        })
    return cards, radni


# ---------------------------------------------------------------------------
# PDF parsing (radny, data_wplywu)
# ---------------------------------------------------------------------------

_DATE_RES = [
    re.compile(r"dnia\s*(\d{1,2})[\.\-](\d{1,2})[\.\-](20\d{2})"),
    re.compile(r"Gliwice[,\s]+(\d{1,2})[\.\-/](\d{1,2})[\.\-/](20\d{2})"),
    re.compile(r"\b(\d{1,2})[\.\-/](\d{1,2})[\.\-/](20\d{2})\b"),
]


def _extract_date(t: str) -> str:
    for rx in _DATE_RES:
        m = rx.search(t)
        if m:
            d, mo, y = m.groups()
            try:
                return f"{y}-{int(mo):02d}-{int(d):02d}"
            except ValueError:
                continue
    return ""


def _match_radny(t: str, radni: list[str]) -> str:
    low = t.lower()
    # Najpierw dopasowanie dokładnych nazwisk/kombinacji z dropdownu.
    for name in radni:
        parts = [p for p in name.lower().split() if p]
        if not parts:
            continue
        # Pełne imię+nazwisko albo (gdy PDF ma inicjał) nazwisko.
        if name.lower() in low:
            return name
    # Nazwisko (ostatni człon) — uchwyci też warianty z inicjałem.
    for name in radni:
        surname = name.split()[-1].lower()
        if len(surname) > 3 and surname in low:
            # wymagaj kontekstu radnego by nie łapać nazwisk w treści
            for ctx in ("radny", "radna", "rada miasta gliwice", "rada"):
                if ctx in low:
                    return name
    return ""


def parse_pdf(t: str, radni: list[str]) -> tuple[str, str]:
    """(radny, data_wplywu) z tekstu PDF."""
    data = _extract_date(t)
    radny = _match_radny(t, radni)
    return radny, data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Gliwice"
    )
    parser.add_argument("--output", required=True, type=Path, help="Plik JSON")
    parser.add_argument("--cache-dir", required=True, type=Path, help="Katalog cache")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też VIII kadencję (rok<2024); domyślnie tylko 2024+",
    )
    parser.add_argument("--skip-pdfs", action="store_true",
                        help="Nie pobieraj PDF-ów (brak radny/dat), tylko HTML")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    _DEBUG = args.debug
    init_cache(args.cache_dir)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Interpelacje — BIP Gliwice ({LIST_URL}) ===")

    html = fetch_text(LIST_URL)
    if not html:
        print("BŁĄD: brak treści listingu. Sprawdź URL / dostępność.")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text("[]", encoding="utf-8")
        return 1

    cards, radni = parse_listing(html)
    print(f"  Listing: {len(cards)} rekordów; radni w dropdownie: {len(radni)}")
    if not cards:
        print("  Brak rekordów — uczciwy pusty wynik (nie zmyślam).")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text("[]", encoding="utf-8")
        return 0

    records = []
    matched_radny = 0
    matched_date = 0
    for i, c in enumerate(cards, start=1):
        radny, data_wplywu = "", ""
        if not args.skip_pdfs and c["tresc_url"]:
            _, t = fetch_pdf(args.cache_dir, c["tresc_url"])
            if t:
                radny, data_wplywu = parse_pdf(t, radni)
                if radny:
                    matched_radny += 1
                if data_wplywu:
                    matched_date += 1
        try:
            rok = int(data_wplywu[:4]) if data_wplywu else 0
        except ValueError:
            rok = 0
        kadencja = "2024-2029" if rok >= 2024 else ("2018-2024" if rok >= 2018 else "2024-2029")

        if not args.all and rok and rok < MIN_ROK_DEFAULT:
            # VIII kadencja — pomiń w domyślnym przebiegu (--all je włącza).
            continue

        records.append({
            "cri": c["cri"],
            "typ": c["typ"],
            "rok": rok,
            "kadencja": kadencja,
            "radny": radny,
            "przedmiot": c["przedmiot"],
            "data_wplywu": data_wplywu,
            "klub": _club_for_radny(radny),
            "odpowiedz_status": "Udzielono" if c["odpowiedz_url"] else "Nie udzielono",
            "tresc_url": c["tresc_url"],
            "odpowiedz_url": c["odpowiedz_url"],
            "data_odpowiedzi": "",
            "bip_url": c["bip_url"],
        })
        if i % 100 == 0:
            print(f"  przetworzono {i}/{len(cards)}")

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    with_radny = sum(1 for r in records if r["radny"])
    with_data = sum(1 for r in records if r["data_wplywu"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:   {interp}")
    print(f"Zapytania:      {zap}")
    print(f"Z odpowiedzią:  {answered}")
    print(f"Razem:          {len(records)}")
    print(f"Z radnym (PDF): {with_radny}")
    print(f"Z datą (PDF):   {with_data}")

    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
