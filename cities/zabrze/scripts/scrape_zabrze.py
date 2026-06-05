#!/usr/bin/env python3
"""
Radoskop Zabrze — BIP scraper (imienne głosowania z ZIP-ów protokołów).

Zabrze NIE ma publicznego portalu eSesja, ale używa eSesja wewnętrznie i
publikuje wyniki na BIP (bip.miastozabrze.pl) jako ZIP per sesja. ZIP zawiera
folder "Wyniki głosowań/" z jednym PDF na głosowanie w formacie eSesja
"Wyniki głosowania":

    Wyniki głosowania
    Głosowano w sprawie: <temat>
    ZA: 19, PRZECIW: 4, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 1, NIEOBECNI: 1
    Wyniki imienne:
    ZA (19)
    Imię Nazwisko, Imię Nazwisko, ...
    PRZECIW (4)
    ...
    Głosowanie zakończono w dniu: 27 kwietnia 2026, o godz. 15:03

Architektura:
  1. Lista sesji per rok: /rm/rm_sesje/sesje_{YEAR}, linki do podstron sesji
     /rm/rm_sesje/sesje_{YEAR}/{slug}.
  2. Strona sesji: tytuł "Sesja Rady Miasta w dniu DD miesiąca YYYY r." +
     załącznik ZIP "Protokół z {ROMAN} sesji ..." pod /attachment/{id}.
  3. ZIP → "Wyniki głosowań/*.pdf" → parser per głosowanie.
  4. Output: data.json + kadencja-{id}.json + profiles.json (format Radoskop).

Skład 25 radnych + kluby wg BIP (stan 2026-05-30):
  /rm/sklad_2024, /rm/kluby_radnych_2024_2029. Klub "Dla Zabrza" rozwiązany
  15.01.2026 → jego radni jako Niezrzeszeni.

Użycie:
  python3 scrape_zabrze.py --output docs/data.json --profiles docs/profiles.json
                           [--cache-dir <scratch>/.cache] [--max-sessions N] [--debug]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)

# bip.miastozabrze.pl na runtime NAS zwraca CERTIFICATE_VERIFY_FAILED (CA bundle
# kontenera nie ma pośredniego cert). Jak w scrape_olsztyn.py: wyłączamy verify
# tylko dla tego scrapera i wyciszamy ostrzeżenie. Okresowo sprawdzać czy cert
# chain BIP-u się poprawił.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_VERIFY_TLS = False
try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4")
    sys.exit(1)
try:
    import pdfplumber
except ImportError:
    print("Zainstaluj: pip install pdfplumber")
    sys.exit(1)


BIP_BASE = "https://bip.miastozabrze.pl"
SESSION_YEAR_TPL = f"{BIP_BASE}/rm/rm_sesje/sesje_{{year}}"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}
DEFAULT_KADENCJA = "2024-2029"
KADENCJA_START_YEAR = 2024

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://zabrze.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html,application/zip,*/*",
}
DELAY = 0.3
TIMEOUT = 60

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

# Skład Rady Miasta Zabrze IX kadencji (25 radnych).
# Kod = AKTUALNY klub radnych wg BIP /rm/kluby_radnych_2024_2029 (stan 2026-05-30).
# "Dla Zabrza" rozwiązany 15.01.2026 → Dziębowski/Chrzęstek-Bar/Walerjański
# jako Niezrzeszeni; Bieniek/Jonecko/Śliwa bez klubu.
COUNCILORS: dict[str, str] = {
    # Klub Radnych Koalicja Obywatelska-Nowe Zabrze (11)
    "Alojzy Cieśla": "KO-Nowe Zabrze",
    "Adam Harasimowicz": "KO-Nowe Zabrze",
    "Grzegorz Olejniczak": "KO-Nowe Zabrze",
    "Mariola Olichwer": "KO-Nowe Zabrze",
    "Urszula Potyka": "KO-Nowe Zabrze",
    "Anna Sosnowska": "KO-Nowe Zabrze",
    "Lucyna Langer": "KO-Nowe Zabrze",
    "Artur Libor": "KO-Nowe Zabrze",
    "Wioletta Szymańska": "KO-Nowe Zabrze",
    "Marian Rau": "KO-Nowe Zabrze",
    "Wojciech Niezgoda": "KO-Nowe Zabrze",
    # Klub Radnych Prawo i Sprawiedliwość (4)
    "Martyna Francikowska": "PiS",
    "Adam Ilewski": "PiS",
    "Ferdynand Reiss": "PiS",
    "Grzegorz Turek": "PiS",
    # Klub Radnych Przyjazne Zabrze (4)
    "Grzegorz Lubowiecki": "Przyjazne Zabrze",
    "Paweł Front": "Przyjazne Zabrze",
    "Marcin Szczerba": "Przyjazne Zabrze",
    "Maciej Zgrzendek": "Przyjazne Zabrze",
    # Niezrzeszeni (6): b. Klub Dla Zabrza (rozwiązany 15.01.2026) + bez klubu
    "Sebastian Dziębowski": "Niezrzeszeni",
    "Łucja Chrzęstek-Bar": "Niezrzeszeni",
    "Dariusz Walerjański": "Niezrzeszeni",
    "Janusz Bieniek": "Niezrzeszeni",
    "Krystian Jonecko": "Niezrzeszeni",
    "Maciej Śliwa": "Niezrzeszeni",
}


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

_CACHE_DIR: Path | None = None


def init_cache(path: str | None) -> None:
    global _CACHE_DIR
    _CACHE_DIR = Path(path) if path else None
    if _CACHE_DIR:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_html(http: requests.Session, url: str, use_cache: bool = True, debug: bool = False) -> str:
    cache_p = None
    if _CACHE_DIR is not None and use_cache:
        cache_p = _CACHE_DIR / f"{hashlib.md5(url.encode()).hexdigest()[:16]}.html"
        if cache_p.exists():
            return cache_p.read_text(encoding="utf-8")
    if debug:
        print(f"  GET {url}")
    resp = http.get(url, headers=HEADERS, timeout=TIMEOUT, verify=_VERIFY_TLS)
    resp.raise_for_status()
    text = resp.text
    if cache_p is not None:
        cache_p.write_text(text, encoding="utf-8")
    time.sleep(DELAY)
    return text


def fetch_zip(http: requests.Session, url: str, debug: bool = False) -> bytes | None:
    if debug:
        print(f"      GET ZIP {url}")
    try:
        resp = http.get(url, headers=HEADERS, timeout=TIMEOUT, verify=_VERIFY_TLS)
        resp.raise_for_status()
        time.sleep(DELAY)
        return resp.content
    except Exception as exc:
        print(f"      BŁĄD pobierania ZIP {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Name resolution (order-insensitive)
# ---------------------------------------------------------------------------

def _normalize_name_for_match(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip().lower()) if p]
    return " ".join(sorted(parts))


_NAME_LOOKUP: dict[str, str] = {}


def build_name_lookup() -> dict[str, str]:
    return {_normalize_name_for_match(n): n for n in COUNCILORS}


def resolve_canonical_name(name: str) -> str | None:
    return _NAME_LOOKUP.get(_normalize_name_for_match(name))


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

DATE_IN_TITLE_RE = re.compile(
    r"w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE
)
ROMAN_IN_ZIP_RE = re.compile(r"Protok[oó][lł]\s+z\s+([IVXLCDM]+)\s+sesj", re.IGNORECASE)


def discover_sessions(http: requests.Session, debug: bool = False, max_sessions: int = 0) -> list[dict]:
    """Zbiera podstrony sesji z list rocznych /rm/rm_sesje/sesje_{YEAR}."""
    sessions: dict[str, dict] = {}
    this_year = date.today().year
    for year in range(KADENCJA_START_YEAR, this_year + 1):
        url = SESSION_YEAR_TPL.format(year=year)
        try:
            html = fetch_html(http, url, use_cache=False, debug=debug)
        except Exception as exc:
            if debug:
                print(f"  rok {year}: {exc}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        pat = re.compile(rf"/rm/rm_sesje/sesje_{year}/.+")
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0].split("?")[0]
            full = href if href.startswith("http") else BIP_BASE + href
            rel = full.replace(BIP_BASE, "")
            if not pat.fullmatch(rel) or rel.endswith(f"/sesje_{year}"):
                continue
            if full in sessions:
                continue
            sessions[full] = {"url": full, "year": year}
        if debug:
            print(f"  rok {year}: łącznie {len(sessions)} sesji")
    out = list(sessions.values())
    if max_sessions:
        out = out[:max_sessions]
    return out


def parse_session_page(http: requests.Session, sess: dict, debug: bool = False) -> dict | None:
    """Wyciąga datę, numer (rzymski) i URL ZIP-a z protokołem głosowań."""
    try:
        html = fetch_html(http, sess["url"], use_cache=True, debug=debug)
    except Exception as exc:
        print(f"  BŁĄD strony sesji {sess['url']}: {exc}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = soup.get_text(" ", strip=True)

    sess_date = ""
    dm = DATE_IN_TITLE_RE.search(title) or DATE_IN_TITLE_RE.search(page_text)
    if dm:
        month = MONTHS_PL.get(dm.group(2).lower())
        if month:
            sess_date = f"{int(dm.group(3)):04d}-{month:02d}-{int(dm.group(1)):02d}"

    zip_url = ""
    number = ""
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if "/attachment/" in a["href"] and re.search(r"protok", text, re.IGNORECASE) and "sesj" in text.lower():
            zip_url = a["href"] if a["href"].startswith("http") else BIP_BASE + a["href"]
            rm = ROMAN_IN_ZIP_RE.search(text)
            if rm:
                number = rm.group(1).upper()
            break

    if not zip_url:
        if debug:
            print(f"      brak ZIP-a protokołu w {sess['url']}")
        return None
    return {"url": sess["url"], "date": sess_date, "number": number, "zip_url": zip_url}


# ---------------------------------------------------------------------------
# Per-vote PDF parser (format eSesja "Wyniki głosowania")
# ---------------------------------------------------------------------------

_VOTE_TOKEN = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "BRAK GŁOSU": "brak_glosu",
    "NIEOBECNI": "nieobecni",
}
_COUNT_RE = re.compile(
    r"ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*"
    r"BRAK GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)"
)
_TOPIC_RE = re.compile(r"Głosowano w sprawie:\s*(.*?)\s*ZA:\s*\d+,", re.S)
_CAT_HDR_RE = re.compile(
    r"^(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\(\d+\)\s*$", re.M
)


def parse_vote_pdf(data: bytes, debug: bool = False) -> dict | None:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as exc:
        if debug:
            print(f"        BŁĄD PDF: {exc}")
        return None

    mc = _COUNT_RE.search(text)
    if not mc:
        return None
    counts = dict(zip(
        ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"],
        (int(x) for x in mc.groups()),
    ))

    topic = ""
    mt = _TOPIC_RE.search(text)
    if mt:
        topic = re.sub(r"\s+", " ", mt.group(1)).strip(" ;")

    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
    imienne = text.split("Wyniki imienne:", 1)[1] if "Wyniki imienne:" in text else ""
    imienne = imienne.split("Głosowanie zakończono")[0]
    hdrs = list(_CAT_HDR_RE.finditer(imienne))
    for i, m in enumerate(hdrs):
        key = _VOTE_TOKEN[m.group(1)]
        seg = imienne[m.end():hdrs[i + 1].start() if i + 1 < len(hdrs) else len(imienne)]
        for raw in seg.split(","):
            nm = re.sub(r"\s+", " ", raw).strip()
            if not nm:
                continue
            named[key].append(resolve_canonical_name(nm) or nm)

    return {"topic": topic, "counts": counts, "named_votes": named}


def _pdf_sort_key(name: str) -> tuple[int, str]:
    """Sortuj PDFy po wiodącym numerze w nazwie pliku ("1. ...", "10. ...")."""
    base = name.rsplit("/", 1)[-1]
    m = re.match(r"\s*(\d+)\.", base)
    return (int(m.group(1)) if m else 999, base.lower())


def parse_session_votes(zip_bytes: bytes, sess: dict, debug: bool = False) -> list[dict]:
    votes: list[dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as exc:
        print(f"      BŁĄD otwierania ZIP: {exc}")
        return []
    pdf_names = [n for n in zf.namelist()
                 if "wyniki głosowa" in n.lower() and n.lower().endswith(".pdf")]
    pdf_names.sort(key=_pdf_sort_key)
    for idx, name in enumerate(pdf_names, 1):
        parsed = parse_vote_pdf(zf.read(name), debug=debug)
        if not parsed:
            continue
        num_part = f"_{sess['number']}" if sess.get("number") else ""
        votes.append({
            "id": f"{sess['date']}{num_part}_{idx:03d}",
            "session_number": sess.get("number", ""),
            "session_date": sess["date"],
            "topic": parsed["topic"][:300] if parsed["topic"] else f"Głosowanie nr {idx}",
            "counts": parsed["counts"],
            "named_votes": parsed["named_votes"],
        })
    if debug:
        print(f"      {len(votes)} głosowań z {len(pdf_names)} PDF")
    return votes


# ---------------------------------------------------------------------------
# Build output (format jak olsztyn/czestochowa — zweryfikowany w verify_city)
# ---------------------------------------------------------------------------

# Kanoniczny slugifier wspólny dla całego projektu — patrz
# radoskop/scripts/lib_slug.py (identyczne wyniki dla polskich nazwisk).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_slug import make_slug as slugify  # noqa: E402


def build_profiles(votes: list[dict]) -> list[dict]:
    profiles = {}
    for name, club in COUNCILORS.items():
        profiles[name] = {
            "name": name, "slug": slugify(name),
            "kadencje": {DEFAULT_KADENCJA: {
                "club": club, "club_full": club,
                "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
                "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                "votes_total": 0, "rebellion_count": 0, "rebellions": [],
            }},
        }
    total = len(votes)
    for v in votes:
        for key, names in v.get("named_votes", {}).items():
            for n in names:
                if n not in profiles:
                    continue
                kd = profiles[n]["kadencje"][DEFAULT_KADENCJA]
                if key == "za":
                    kd["votes_za"] += 1
                elif key == "przeciw":
                    kd["votes_przeciw"] += 1
                elif key == "wstrzymal_sie":
                    kd["votes_wstrzymal"] += 1
    for p in profiles.values():
        kd = p["kadencje"][DEFAULT_KADENCJA]
        active = kd["votes_za"] + kd["votes_przeciw"] + kd["votes_wstrzymal"]
        kd["votes_total"] = active
        if total > 0:
            kd["frekwencja"] = round(100 * active / total, 1)
            kd["aktywnosc"] = round(100 * active / total, 1)
    return list(profiles.values())


def _build_clubs_summary(councilors: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for c in councilors:
        club = c.get("club", "")
        if not club:
            continue
        out.setdefault(club, {"members": 0, "members_list": []})
        out[club]["members"] += 1
        out[club]["members_list"].append(c["name"])
    return out


def build_outputs(sessions: list[dict], votes: list[dict], output_path: Path, profiles_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profiles_path.parent.mkdir(parents=True, exist_ok=True)

    sessions_out = []
    for s in sessions:
        s_votes = [v for v in votes if v["session_date"] == s["date"]
                   and v["session_number"] == s.get("number", "")]
        attendees = set()
        for v in s_votes:
            for key, names in v.get("named_votes", {}).items():
                if key in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
                    attendees.update(names)
        sessions_out.append({
            "number": s.get("number", ""),
            "date": s["date"],
            "url": s["url"],
            "vote_count": len(s_votes),
            "attendee_count": len(attendees),
            "results_pending": len(s_votes) == 0,
        })

    profiles = build_profiles(votes)
    councilors = [{"name": p["name"], "slug": p["slug"], **p["kadencje"][DEFAULT_KADENCJA]} for p in profiles]

    kad_data = {
        "id": DEFAULT_KADENCJA,
        "label": KADENCJE[DEFAULT_KADENCJA]["label"],
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": sessions_out,
        "votes": votes,
        "councilors": councilors,
        "total_sessions": len(sessions_out),
        "total_votes": len(votes),
        "total_councilors": len(councilors),
        "clubs": _build_clubs_summary(councilors),
    }
    data_index = {
        "default_kadencja": DEFAULT_KADENCJA,
        "kadencje": [{"id": DEFAULT_KADENCJA, "label": KADENCJE[DEFAULT_KADENCJA]["label"]}],
    }
    output_path.write_text(json.dumps(data_index, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_path.parent / f"kadencja-{DEFAULT_KADENCJA}.json").write_text(
        json.dumps(kad_data, ensure_ascii=False, indent=2), encoding="utf-8")
    profiles_path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== Wyniki ===")
    print(f"Sesji:    {len(sessions_out)}")
    print(f"Głosowań: {len(votes)}")
    print(f"Radnych:  {len(councilors)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(output_path: Path, profiles_path: Path, max_sessions: int = 0, debug: bool = False) -> None:
    global _NAME_LOOKUP
    _NAME_LOOKUP = build_name_lookup()
    print(f"=== Radoskop Zabrze (BIP ZIP) === radnych: {len(COUNCILORS)}")

    http = requests.Session()
    print("[1/3] Lista sesji")
    raw_sessions = discover_sessions(http, debug=debug, max_sessions=max_sessions)
    print(f"  Znaleziono podstron sesji: {len(raw_sessions)}")

    print("[2/3] Pobieranie ZIP-ów i parsowanie głosowań")
    sessions: list[dict] = []
    all_votes: list[dict] = []
    for i, rs in enumerate(raw_sessions, 1):
        meta = parse_session_page(http, rs, debug=debug)
        if not meta or not meta["date"]:
            continue
        if meta["date"] < KADENCJE[DEFAULT_KADENCJA]["start"]:
            continue
        zip_bytes = fetch_zip(http, meta["zip_url"], debug=debug)
        if not zip_bytes:
            sessions.append(meta)
            continue
        votes = parse_session_votes(zip_bytes, meta, debug=debug)
        sessions.append(meta)
        all_votes.extend(votes)
        print(f"  [{i}/{len(raw_sessions)}] {meta['date']} ({meta.get('number','?')}): {len(votes)} głosowań")

    print("[3/3] Składanie outputów")
    build_outputs(sessions, all_votes, output_path, profiles_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Radoskop Zabrze (BIP ZIP scraper)")
    ap.add_argument("--output", default="docs/data.json")
    ap.add_argument("--profiles", default="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    init_cache(args.cache_dir)
    if args.dry_run:
        global _NAME_LOOKUP
        _NAME_LOOKUP = build_name_lookup()
        http = requests.Session()
        sess = discover_sessions(http, debug=args.debug, max_sessions=args.max_sessions)
        print(f"Znaleziono {len(sess)} podstron sesji:")
        for s in sess:
            print(f"  {s['url']}")
        return 0
    try:
        scrape(Path(args.output), Path(args.profiles),
               max_sessions=args.max_sessions, debug=args.debug)
    except Exception as exc:
        print(f"BŁĄD: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
