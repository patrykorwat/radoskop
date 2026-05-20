#!/usr/bin/env python3
"""
Scraper Rīgas domes balsojumi via PKIP API + OCR PDF protokołów.

WAŻNE OGRANICZENIE: Rīgas dome publikuje balsošanas protokols jako
SKANY z drukarki Canon iR-ADV. Per-radny głos jest reprezentowany
odręcznym podpisem + przekreśleniem nieobecnych nazwisk. Tekst PDF
jest pusty (pdftotext zwraca 0 bajtów). Dlatego ten scraper produkuje
TYLKO AGREGATY per głosowanie (counts Par/Pret/Atturas, wynik, tytuł,
timestamp), bez per-radny attribution. To znaczne ograniczenie w
porównaniu z polskimi miastami i Tallinem (TEELE API).

Frakcje per radny są przyporządkowane z zewn. mapping w
`cities/riga/deputati_2025_2029.json`, bo PDF tej info nie ma.
Frekwencja per radny per sesja NIE jest zbierana (brak parser per-radny).

Pipeline:
1. GET https://pkip.riga.lv/agendaitempublish/cards
   Filtruj cards po id == 241 ("Rīgas dome", rada miejska).
   Komisje (cards id 281/321/341/361/381/382/401/402/501) odpadają.
2. Z card[241].meetings filtruj po dateTime >= kadencja_start.
3. Per meeting GET /meeting/{id} → agenda[]
4. Per agenda item: jeśli ma votingProtocols, pobierz PDF.
5. OCR górny ~250px PDF (tesseract -l lav --psm 6) →
   "Pieņemts/Noraidīts Par: N Pret: N Atturas: N"
   plus datum + nadtytuł (RD-25-X-lp).
   resultOfTheVote bierzemy z JSON (gotowe), counts z OCR.
6. Zapisz docs/kadencja-{id}.json (sessions, votes, councilor_index).

Cache:
- PDF cached po sha256(link) w .cache/pdfs/
- OCR text cached po sha256(pdf) w .cache/ocr/

Wymaga zewn. tooli:
- pdftoppm (poppler-utils) — PDF → PNG
- tesseract (tesseract-ocr-lav opcjonalnie, fallback do tesseract-ocr-eng)

Użycie:
    python3 scrape_balsojumi.py
    python3 scrape_balsojumi.py --max-sessions 2
    python3 scrape_balsojumi.py --skip-fetch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
# Deputati JSON: source-of-truth mapowania radny → klub. Plik leży na
# poziomie cities/riga/ (nie w data/ bo data/ jest w .gitignore i nie
# trafiał do repo). Backward compat: jeśli plik na poziomie city nie
# istnieje, fallback do starej ścieżki cities/riga/data/.
_DEPUTATI_NEW = CITY_DIR / "deputati_2025_2029.json"
_DEPUTATI_OLD = CITY_DIR / "data" / "deputati_2025_2029.json"
DEFAULT_DEPUTATI = _DEPUTATI_NEW if _DEPUTATI_NEW.is_file() else _DEPUTATI_OLD
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache"

USER_AGENT = "Mozilla/5.0 Radoskop/1.0 (+https://radoskop.eu)"
TIMEOUT = 60
RETRY_COUNT = 3
SLEEP_BETWEEN_CALLS = 0.1

PKIP_API_BASE = "https://pkip.riga.lv/agendaitempublish"
RIGAS_DOME_CARD_ID = 241  # twardy filtr: tylko rada miejska, nie komisje

# OCR config
OCR_LANG = "lav"  # tesseract-ocr-lav; fallback do "eng" jeśli brak
OCR_HEADER_PIXELS = 350  # górne 350px PDF zawiera tytuł + counts + datum
PDFTOPPM_DPI = 200

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

# Mapowanie wyniku z JSON resultOfTheVote → kategoria Radoskop.
RESULT_MAP = {
    "Pieņemts": "PRZYJETE",
    "Pienemts": "PRZYJETE",  # fallback bez diakrytyki
    "Noraidīts": "ODRZUCONE",
    "Noraidits": "ODRZUCONE",
    "Atlikts": "ODROCZONE",
    "Nebalsoja": "ODROCZONE",
}


def _cache_key(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def http_get_json(url: str, cache_dir: Path | None) -> Any:
    """GET JSON z retry i cache dyskowym."""
    cache_file: Path | None = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{_cache_key(url)}.json"
        if cache_file.is_file():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cache_file.unlink()

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            break
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    else:
        raise RuntimeError(f"Failed after {RETRY_COUNT} attempts: {last_err}")

    data = json.loads(raw)
    if cache_file:
        cache_file.write_text(raw, encoding="utf-8")
    time.sleep(SLEEP_BETWEEN_CALLS)
    return data


def http_download(url: str, target: Path) -> bool:
    """Download binary file. Returns True on success."""
    if target.is_file() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                target.write_bytes(resp.read())
            time.sleep(SLEEP_BETWEEN_CALLS)
            return True
        except (HTTPError, URLError, TimeoutError) as exc:
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{RETRY_COUNT} after {wait}s ({exc})",
                  file=sys.stderr)
            time.sleep(wait)
    return False


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def have_command(name: str) -> bool:
    return shutil.which(name) is not None


def pdf_to_png(pdf_path: Path, out_dir: Path) -> Path | None:
    """Konwertuje pierwszą stronę PDF → PNG za pomocą pdftoppm.

    Zwraca ścieżkę do PNG albo None.
    """
    if not have_command("pdftoppm"):
        print("UWAGA: brak pdftoppm (zainstaluj poppler-utils)", file=sys.stderr)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = out_dir / pdf_path.stem
    target = Path(f"{out_prefix}-1.png")
    if target.is_file():
        return target
    try:
        subprocess.run(
            ["pdftoppm", "-r", str(PDFTOPPM_DPI), "-f", "1", "-l", "1",
             "-png", str(pdf_path), str(out_prefix)],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  pdftoppm failed: {exc}", file=sys.stderr)
        return None
    return target if target.is_file() else None


def ocr_header(png_path: Path) -> str:
    """OCR górnych OCR_HEADER_PIXELS pikseli obrazu.

    Tesseract z lav language preferowany, fallback do eng.
    """
    if not have_command("tesseract"):
        print("UWAGA: brak tesseract", file=sys.stderr)
        return ""

    # Wyciągnij górną sekcję obrazu (header). PIL byłby wygodniejszy ale
    # chcemy unikać dodatkowych deps. ImageMagick `convert` często jest
    # już zainstalowany razem z poppler. Fallback: OCR cały obraz.
    cropped = png_path
    if have_command("convert"):
        cropped = png_path.with_suffix(".header.png")
        if not cropped.is_file():
            try:
                # crop górnych OCR_HEADER_PIXELS pikseli
                subprocess.run(
                    ["convert", str(png_path),
                     "-crop", f"100%x{OCR_HEADER_PIXELS}+0+0", "+repage",
                     str(cropped)],
                    check=True, capture_output=True, timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                cropped = png_path

    # Spróbuj OCR z lav, fallback eng.
    for lang in (OCR_LANG, "eng"):
        try:
            result = subprocess.run(
                ["tesseract", str(cropped), "-", "-l", lang, "--psm", "6"],
                check=True, capture_output=True, timeout=60, text=True,
            )
            if result.stdout.strip():
                return result.stdout
        except subprocess.CalledProcessError as exc:
            if lang == OCR_LANG and "language" in (exc.stderr or "").lower():
                # lav pakiet nieobecny, próbuj eng
                continue
            print(f"  tesseract ({lang}) failed: {exc.stderr}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(f"  tesseract ({lang}) timeout", file=sys.stderr)
    return ""


def _find_count_near_label(text: str, label_patterns: list[str]) -> int | None:
    """Per-label search z elastycznym sąsiedztwem liczby.

    Łotewski OCR daje różne układy:
      "Par: 43 Pret: 0 Atturas: 0"     (inline)
      "Par 43\nPret 0\nAtturas 0"       (newline)
      "Par\n43\nPret\n0\nAtturas\n0"    (label osobno)
      "43 Par"                          (liczba PRZED label)
      "BALSOJUMI: PAR 43, PRET 0..."   (uppercase)
    Dla każdego patternu label szukamy liczby w ±40 znakach okolicy.
    """
    for pat in label_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            # Najpierw po label (typowo "Par: 43")
            after = text[m.end():m.end() + 40]
            num_after = re.search(r"\b(\d+)\b", after)
            if num_after:
                return int(num_after.group(1))
            # Fallback: liczba przed label (np. "43 par")
            before = text[max(0, m.start() - 20):m.start()]
            num_before = re.search(r"\b(\d+)\b(?!.*\b\d+\b)", before)
            if num_before:
                return int(num_before.group(1))
    return None


def parse_ocr_text(text: str) -> dict[str, Any]:
    """Wyciąga z OCR text: counts (Par/Pret/Atturas), datetime, tytuł."""
    out: dict[str, Any] = {
        "par": None,
        "pret": None,
        "atturas": None,
        "vote_datetime": "",
        "title_fragment": "",
    }
    if not text:
        return out

    # Counts per label z tolerancją na OCR-noise:
    # Par (za): łotewski "par" = "for". OCR często myli "Par" z "Por".
    # Pret (przeciw): mylone z "Pret" / "Prei" / "Preti".
    # Atturas (wstrzymał się): mylone z "Atturas" / "Atturos" / "Atur".
    par = _find_count_near_label(text, [r"\bP[ao]r\b"])
    pret = _find_count_near_label(text, [r"\bP[reti]{2,4}\b"])
    atturas = _find_count_near_label(text, [r"\bAt[tu]?u?r[ao]s?\b"])

    # Fallback: stary monolityczny regex (działa gdy układ jest klasyczny
    # "Par: 43 Pret: 0 Atturas: 0" w jednej linii).
    if par is None or pret is None or atturas is None:
        counts_re = re.compile(
            r"\bP[ao]r[\s:.\-]+(\d+)\s+P[reti]+[\s:.\-]+(\d+)\s+At[tu]?u?r[ao]?s?[\s:.\-]+(\d+)",
            re.IGNORECASE,
        )
        m = counts_re.search(text)
        if m:
            par = par if par is not None else int(m.group(1))
            pret = pret if pret is not None else int(m.group(2))
            atturas = atturas if atturas is not None else int(m.group(3))

    out["par"] = par
    out["pret"] = pret
    out["atturas"] = atturas

    # Datetime: "Datums: 14.01.2026 11:09" lub OCR variants ("Datums" → "Datums" w lav).
    dt_re = re.compile(
        r"(?:Datums|Date)[\s:.\-]+(\d{2})[\.\-/](\d{2})[\.\-/](\d{4})\s+(\d{1,2}):(\d{2})",
        re.IGNORECASE,
    )
    m = dt_re.search(text)
    if m:
        d, mo, y, h, mi = m.groups()
        out["vote_datetime"] = f"{y}-{mo}-{d}T{int(h):02d}:{mi}:00"

    # Tytuł: zaczyna się od "RD-NN-NNN-lp:" lub po "Lēmuma projekts:"
    title_re = re.compile(
        r"RD[\-\s]\d{2}[\-\s]\d+[\-\s]lp[\s:.\-]*([^\n]+)",
        re.IGNORECASE,
    )
    m = title_re.search(text)
    if m:
        out["title_fragment"] = m.group(1).strip()[:250]
    return out


# ---------------------------------------------------------------------------
# PKIP API helpers
# ---------------------------------------------------------------------------

def fetch_cards(cache_dir: Path | None) -> list[dict[str, Any]]:
    url = f"{PKIP_API_BASE}/cards"
    data = http_get_json(url, cache_dir)
    return data.get("cards", []) if isinstance(data, dict) else []


def fetch_meeting(meeting_id: int, cache_dir: Path | None) -> dict[str, Any]:
    url = f"{PKIP_API_BASE}/meeting/{meeting_id}"
    return http_get_json(url, cache_dir) or {}


def parse_meeting_datetime(dt_str: str) -> str:
    """'14.01.2026 11:00' → '2026-01-14T11:00:00'."""
    if not dt_str:
        return ""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\.?\s+(\d{1,2}):(\d{2})", dt_str.strip())
    if not m:
        return ""
    d, mo, y, h, mi = m.groups()
    return f"{y}-{mo}-{d}T{int(h):02d}:{mi}:00"


def meeting_date_iso(dt_str: str) -> str:
    iso = parse_meeting_datetime(dt_str)
    return iso[:10] if iso else ""


def meeting_number_from_title(title: str) -> str:
    """'Rīgas dome 14. sēdes (...)' → '14'."""
    m = re.search(r"(\d+)\.\s*sēd", title or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Build kadencja JSON
# ---------------------------------------------------------------------------

def normalize_result(result_native: str) -> str:
    if not result_native:
        return ""
    for key, mapped in RESULT_MAP.items():
        if key.lower() in result_native.lower():
            return mapped
    return result_native


def build_kadencja(
    raw_votes: list[dict[str, Any]],
    meetings_by_id: dict[int, dict[str, Any]],
    club_assignments: dict[str, str],
    config: dict[str, Any],
    kadencja_id: str,
) -> dict[str, Any]:
    """Buduje kadencja-{id}.json.

    UWAGA: bez per-radny attribution. councilor_index pochodzi z
    deputati_2025_2029.json mapping, named_votes są puste listy.
    """
    kadencje = config.get("kadencje", {})
    kdef = kadencje.get(kadencja_id) or {}
    start_date = kdef.get("start", "")

    # councilor_index = lista nazwisk z mapping (stała kadencyjna).
    councilor_index: list[str] = sorted(club_assignments.keys())

    votes_flat: list[dict[str, Any]] = []
    sessions_meta: dict[str, dict[str, Any]] = {}

    for rv in raw_votes:
        meeting = meetings_by_id.get(rv["meeting_id"]) or {}
        date = meeting_date_iso(meeting.get("dateTime", ""))
        if not date or (start_date and date < start_date):
            continue

        item = rv["item"]
        parsed = rv.get("ocr_parsed", {})
        ocr_counts: dict[str, int] = {c: 0 for c in CATEGORIES}
        if parsed.get("par") is not None:
            ocr_counts["za"] = int(parsed["par"])
        if parsed.get("pret") is not None:
            ocr_counts["przeciw"] = int(parsed["pret"])
        if parsed.get("atturas") is not None:
            ocr_counts["wstrzymal_sie"] = int(parsed["atturas"])
        # nieobecni: 60 - (za + przeciw + wstrzymal_sie + brak_glosu)
        present_sum = ocr_counts["za"] + ocr_counts["przeciw"] + ocr_counts["wstrzymal_sie"]
        if present_sum > 0:
            total = config.get("councilor_count", 60)
            absent = max(0, total - present_sum)
            ocr_counts["nieobecni"] = absent

        vote_id = f"riga_{rv['meeting_id']}_{item.get('id')}"
        source_url = (
            f"https://www.riga.lv/lv/meeting/{rv['meeting_id']}"
            f"?item={item.get('id')}"
        )

        votes_flat.append({
            "id": vote_id,
            "session_date": date,
            "session_number": meeting_number_from_title(meeting.get("title", "")),
            "source_url": source_url,
            "topic": item.get("title", "") or parsed.get("title_fragment", ""),
            "druk": item.get("documentNumber") or "",
            "resolution": "",
            "result_native": item.get("resultOfTheVote", "") or "",
            "result": normalize_result(item.get("resultOfTheVote", "") or ""),
            "counts": ocr_counts,
            "named_votes": {c: [] for c in CATEGORIES},
            "voted_at": parsed.get("vote_datetime", ""),
            "_note": "agregat z PDF protokołu (skan), per-radny vote attribution niedostępne",
        })

        sess = sessions_meta.setdefault(date, {
            "date": date,
            "number": meeting_number_from_title(meeting.get("title", "")),
            "title": meeting.get("title", "") or f"Rīgas domes sēde {date}",
            "start": parse_meeting_datetime(meeting.get("dateTime", "")),
            "end": "",
            "vote_ids": [],
            "attendees": set(councilor_index),  # bez per-radny, zakładamy obecność
        })
        sess["vote_ids"].append(vote_id)

    sessions: list[dict[str, Any]] = []
    for date, sess in sessions_meta.items():
        attendees_list = sorted(sess["attendees"])
        sessions.append({
            "date": date,
            "number": sess["number"],
            "title": sess["title"],
            "start": sess["start"],
            "end": sess["end"],
            "vote_count": len(sess["vote_ids"]),
            "attendee_count": len(attendees_list),
            "attendees": attendees_list,
            "source_url": "https://www.riga.lv/lv/meetings-riga-city-council",
        })
    sessions.sort(key=lambda s: s["date"])

    return {
        "sessions": sessions,
        "votes": votes_flat,
        "councilor_index": councilor_index,
    }


def build_profiles(
    club_assignments: dict[str, str],
) -> dict[str, dict[str, str]]:
    return {
        name: {"name": name, "club": club}
        for name, club in club_assignments.items()
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--deputati", type=Path, default=DEFAULT_DEPUTATI)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--kadencja-id", help="Konkretna kadencja, domyślnie wszystkie z config")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Limit sesji (test mode)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Użyj wyłącznie cache, nie pobieraj świeżych")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Pomijaj OCR (counts będą puste, tylko result/title)")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)
    # Soft-fail jeśli brak pliku deputati. Riga 2025-2029 wymaga ręcznego
    # zbudowania mapowania radny → klub z danych riga.lv (NIE da się
    # zescrapować automatycznie, bo Rīgas dome publikuje protokoły bez
    # klubowości). Bez tego pliku scraper i tak nie ma jak przypisać
    # głosów do partii. Lepiej skip-and-warn niż crash całego pipeline.
    from pathlib import Path as _Path
    if not _Path(args.deputati).is_file():
        print(
            f"[riga] WARN: brak {args.deputati}. "
            "Skipping scrape - zostaw poprzednie data.json (jeśli istnieje).",
            file=sys.stderr,
        )
        print(
            "[riga] Plik deputati JSON musi być zbudowany ręcznie z listy "
            "radnych Rīgas dome (riga.lv/lv/dome/deputati) plus mapowanie "
            "klubowości. Soft-disable do czasu uzupełnienia.",
            file=sys.stderr,
        )
        return 0  # Soft-fail: exit OK, pipeline pójdzie dalej
    with open(args.deputati, "r", encoding="utf-8") as f:
        deputati_data = json.load(f)
    club_assignments: dict[str, str] = deputati_data.get("club_assignments", {})
    if not club_assignments:
        print("[riga] WARN: deputati JSON bez club_assignments, skip scrape", file=sys.stderr)
        return 0

    cache = args.cache if not args.skip_fetch else None
    json_cache = (args.cache / "json") if cache else None
    pdf_cache = args.cache / "pdfs"
    png_cache = args.cache / "pngs"
    ocr_cache = args.cache / "ocr"
    args.docs.mkdir(parents=True, exist_ok=True)

    print(f"[riga] fetch /cards", file=sys.stderr)
    cards = fetch_cards(json_cache)
    rada = [c for c in cards if c.get("id") == RIGAS_DOME_CARD_ID]
    if not rada:
        print(f"[riga] FATAL: card.id={RIGAS_DOME_CARD_ID} nie znaleziony", file=sys.stderr)
        return 1

    rada_meetings = rada[0].get("meetings", [])
    print(f"[riga] {len(rada_meetings)} sesji w card 'Rīgas dome' (wszystkie kadencje)",
          file=sys.stderr)

    # Filtruj po dacie kadencji aktywnej.
    kadencja_id = args.kadencja_id or config.get("kadencja_active", "2025-2029")
    kdef = config.get("kadencje", {}).get(kadencja_id) or {}
    start_date = kdef.get("start", "")

    filtered = []
    for m in rada_meetings:
        date = meeting_date_iso(m.get("dateTime", ""))
        if not date:
            continue
        if start_date and date < start_date:
            continue
        filtered.append(m)
    filtered.sort(key=lambda m: meeting_date_iso(m.get("dateTime", "")))
    print(f"[riga] {len(filtered)} sesji od {start_date}", file=sys.stderr)

    if args.max_sessions:
        filtered = filtered[: args.max_sessions]
        print(f"[riga] LIMIT: scrape {len(filtered)} sesji", file=sys.stderr)

    meetings_by_id = {m["id"]: m for m in filtered}
    raw_votes: list[dict[str, Any]] = []

    for i, meeting in enumerate(filtered, 1):
        meeting_id = meeting["id"]
        date = meeting_date_iso(meeting.get("dateTime", ""))
        print(f"[riga] [{i}/{len(filtered)}] sesja {date} (id={meeting_id})",
              file=sys.stderr)
        try:
            detail = fetch_meeting(meeting_id, json_cache)
        except Exception as exc:
            print(f"  ERR fetch meeting: {exc}", file=sys.stderr)
            continue

        agenda = detail.get("agenda", []) or []
        for item in agenda:
            votings = item.get("votingProtocols") or []
            if not votings:
                continue
            ocr_parsed: dict[str, Any] = {}

            if not args.no_ocr:
                # Bierzemy pierwszy protokoł (zwykle jeden per item).
                vp = votings[0]
                link = vp.get("link")
                if not link:
                    continue
                pdf_url = f"{PKIP_API_BASE}/downloadfile/{link}"
                pdf_path = pdf_cache / f"{_cache_key(link)}.pdf"
                if not http_download(pdf_url, pdf_path):
                    print(f"  PDF download FAILED: {link}", file=sys.stderr)
                else:
                    # OCR cache: jeśli mamy text dla tego pdf hash, użyj.
                    ocr_text_path = ocr_cache / f"{pdf_path.stem}.txt"
                    if ocr_text_path.is_file():
                        text = ocr_text_path.read_text(encoding="utf-8")
                    else:
                        png = pdf_to_png(pdf_path, png_cache)
                        text = ocr_header(png) if png else ""
                        ocr_cache.mkdir(parents=True, exist_ok=True)
                        ocr_text_path.write_text(text, encoding="utf-8")
                    ocr_parsed = parse_ocr_text(text)
                    # Debug: jeśli parsowanie nie wyciągnęło żadnego count,
                    # zapisz sample text żeby user mógł sprawdzić format
                    # OCR. Tylko pierwsze 3 puste w runie żeby nie zaśmiecać.
                    if (ocr_parsed.get("par") is None
                            and ocr_parsed.get("pret") is None
                            and ocr_parsed.get("atturas") is None
                            and text):
                        debug_path = args.cache / "ocr_debug_unparseable"
                        debug_path.mkdir(exist_ok=True)
                        # Limit do 5 sampleów per run
                        existing = list(debug_path.glob("*.txt"))
                        if len(existing) < 5:
                            sample = debug_path / f"{pdf_path.stem}.txt"
                            sample.write_text(text[:2000], encoding="utf-8")

            raw_votes.append({
                "meeting_id": meeting_id,
                "item": item,
                "ocr_parsed": ocr_parsed,
            })

    print(f"[riga] zebrano {len(raw_votes)} głosowań", file=sys.stderr)

    # Posprzątaj stare kadencje.
    valid_ids = set(config.get("kadencje", {}).keys())
    for old in args.docs.glob("kadencja-*.json"):
        kid = old.stem.replace("kadencja-", "")
        if kid not in valid_ids:
            old.unlink()

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kadencje_to_generate = (
        [args.kadencja_id]
        if args.kadencja_id
        else list(config.get("kadencje", {}).keys())
    )

    for kid in kadencje_to_generate:
        kdef = config["kadencje"][kid]
        built = build_kadencja(raw_votes, meetings_by_id, club_assignments, config, kid)
        if not built["votes"]:
            print(f"[riga] skip kadencja-{kid}: 0 głosowań", file=sys.stderr)
            continue
        out = {
            "id": kid,
            "label": kdef.get("label", kid),
            "scraped_at": scraped_at,
            "sessions": built["sessions"],
            "votes": built["votes"],
            "councilor_index": built["councilor_index"],
            "_note": (
                "Per-radny vote attribution NIEDOSTĘPNE — Rīgas dome publikuje "
                "balsošanas protokols jako skany z podpisami odręcznymi. counts "
                "(Par/Pret/Atturas) z OCR górnego paska PDF, frakcje z zewn. mapping."
            ),
        }
        out_path = args.docs / f"kadencja-{kid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            f"[riga] wrote {out_path.name}: "
            f"{len(built['sessions'])} sesji, "
            f"{len(built['votes'])} głosowań, "
            f"{len(built['councilor_index'])} radnych",
            file=sys.stderr,
        )

    profiles = build_profiles(club_assignments)
    profiles_path = args.docs / "profiles.json"
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": scraped_at, "profiles": profiles},
                  f, ensure_ascii=False, indent=2)
    print(f"[riga] wrote profiles.json: {len(profiles)} radnych",
          file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
