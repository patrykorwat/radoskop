#!/usr/bin/env python3
"""Scraper namentliche Abstimmungen Stadtvertretung Schwerin.

Źródło: bis.schwerin.de (SessionNet Somacos v5.5.4). Schwerin publikuje
Niederschrift jako PDF tekstowy (summary only, bez per-name), a osobne
Anlagen "Anlage zur Niederschrift - namentliche Abstimmung zu TOP NN
[Beschlusspunkt N]" są skanami JPEG obróconymi o 90° w PDF kontenerze.

Workflow:
1. Pobierz listę sesji z si0042 (gremium = Stadtvertretung kgrnr=1)
2. Per sesja si0057, znajdź załączniki ze słowem "namentliche Abstimmung"
3. Pobierz Anlage PDF
4. OCR: pdf2image -> rotacja 270° -> tesseract deu -> parser tabeli

OCR jest kosztowny (PDF 7 stron skan = ~30s tesseract). Cache PDF i raw
OCR text na dysku absolutnie konieczny.

Output: docs/data.json + docs/kadencja-2024-2029.json.

Wymaga: requests, beautifulsoup4, pdf2image, pytesseract, pdfplumber.
System: tesseract-ocr-deu, poppler-utils.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from hashlib import md5
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


def _ensure_ocr_deps() -> None:
    """Lazy-install pdf2image + pytesseract jeśli brak.

    Docker image NAS od czasu wersji 2026-05-19 ma te pakiety w pip
    install layer. Ale jeśli image jest stary (sprzed rebuild), pull
    radoskop-premium repo da nowy scrape_schwerin.py który nie umie
    uruchomić OCR bez tych libów. Lazy install pokrywa lukę między
    bumpem skryptu a rebuild image.

    UWAGA: tesseract-ocr-deu jest binary apt, lazy install w pythonie
    go nie zainstaluje. Bez rebuild image scrape Schwerina padnie z
    'TesseractError: Cannot recognize language deu'.
    """
    import subprocess
    try:
        import pdf2image  # noqa: F401
    except ImportError:
        subprocess.run(
            ["pip", "install", "--quiet", "--break-system-packages", "pdf2image"],
            check=False,
        )
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        subprocess.run(
            ["pip", "install", "--quiet", "--break-system-packages", "pytesseract"],
            check=False,
        )


_ensure_ocr_deps()


BASE = "https://bis.schwerin.de"
SESSIONS_URL = f"{BASE}/si0042.asp?__kgrnr=1"
SESSION_TPL = f"{BASE}/si0057.asp?__ksinr={{ksinr}}"
FILE_TPL = f"{BASE}/getfile.asp?id={{file_id}}&type=do"
MEMBERS_URL = f"{BASE}/kp0040.asp?__kgrnr=1"

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "Wahlperiode 2024–2029"
KADENCJA_START = date(2024, 7, 8)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
KSINR_RE = re.compile(r"__ksinr=(\d+)")
FILE_ID_RE = re.compile(r"[?&]id=(\d+)")
TOP_RE = re.compile(r"TOP\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
NAMENTLICHE_RE = re.compile(r"namentliche?\s+Abstimmung", re.IGNORECASE)

# Linijka OCR: "Mustermann, Max  CDU  Ja" lub "Mustermann Max CDU Ja"
# Tolerujemy: różne separatory, błędy OCR w fakcji
# Akceptowane decyzje:
DECISION_TOKENS = {
    "Ja": "za",
    "ja": "za",
    "Nein": "przeciw",
    "nein": "przeciw",
    "Enthaltung": "wstrzymal_sie",
    "Enthaltg.": "wstrzymal_sie",
    "Enth.": "wstrzymal_sie",
    "nicht": "brak_glosu",
    "abw.": "nieobecni",
    "Abw.": "nieobecni",
}

KNOWN_CLUBS = {
    "SPD", "CDU", "AfD", "Linke", "Die Linke", "Grüne", "Bündnis 90/Die Grünen",
    "FDP", "Unabhängige Bürger", "UBL", "Volt", "fraktionslos", "Einzel",
}


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return r.content


def list_sessions() -> list[dict]:
    """Pobierz listę sesji Stadtvertretung. Zwraca [{ksinr, date, title}]."""
    html = fetch(SESSIONS_URL)
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = KSINR_RE.search(href)
        if not m:
            continue
        ksinr = m.group(1)
        if ksinr in seen:
            continue
        seen.add(ksinr)
        text = a.get_text(" ", strip=True)
        date_m = DATE_RE.search(text)
        if not date_m:
            continue
        dd, mm, yyyy = date_m.groups()
        d = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        if d < KADENCJA_START.isoformat():
            continue
        out.append({
            "ksinr": ksinr,
            "date": d,
            "title": text,
        })
    return sorted(out, key=lambda s: s["date"])


def find_namentliche_anlagen(ksinr: str, cache_dir: Path | None) -> list[dict]:
    """Per sesja, znajdź załączniki z tytułem "namentliche Abstimmung".

    Zwraca lista [{file_id, top, label}].
    """
    url = SESSION_TPL.format(ksinr=ksinr)
    cache_file = None
    if cache_dir:
        cache_file = cache_dir / f"sess_{ksinr}.html"
        if cache_file.exists() and cache_file.stat().st_size > 100:
            html = cache_file.read_text(encoding="utf-8")
        else:
            html = fetch(url)
            cache_file.write_text(html, encoding="utf-8")
    else:
        html = fetch(url)

    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True)
        if "getfile.asp" not in href:
            continue
        if not NAMENTLICHE_RE.search(label):
            continue
        fid_m = FILE_ID_RE.search(href)
        if not fid_m:
            continue
        top_m = TOP_RE.search(label)
        top = top_m.group(1) if top_m else ""
        out.append({
            "file_id": fid_m.group(1),
            "top": top,
            "label": label,
        })
    return out


def ocr_anlage_pdf(pdf_bytes: bytes, cache_dir: Path | None, file_id: str) -> str:
    """OCR załącznika namentliche.

    Skany w Schwerin są 300dpi A4 obrócone 90 (CW). Tesseract bez rotacji
    daje bełkot. pdf2image konwertuje na PIL Image, rotujemy -90 (czyli
    270 CCW) i lecimy tesseract z lang='deu'.

    Cache: zapisujemy raw OCR text per file_id w cache_dir.
    """
    cache_file = None
    if cache_dir:
        cache_file = cache_dir / f"ocr_{file_id}.txt"
        if cache_file.exists() and cache_file.stat().st_size > 50:
            return cache_file.read_text(encoding="utf-8")

    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as e:
        raise RuntimeError(
            f"OCR wymaga pdf2image + pytesseract. {e}. "
            "Plus apt: tesseract-ocr-deu, poppler-utils."
        )

    images = convert_from_bytes(pdf_bytes, dpi=300)
    text_parts: list[str] = []
    for img in images:
        # Próbujemy 3 orientacje, wybieramy tę z największą liczbą znaków alfanum
        candidates = [img, img.rotate(-90, expand=True), img.rotate(90, expand=True)]
        best_text = ""
        best_score = -1
        for cand in candidates:
            try:
                t = pytesseract.image_to_string(cand, lang="deu")
            except Exception:
                continue
            score = sum(1 for c in t if c.isalnum())
            if score > best_score:
                best_score = score
                best_text = t
        text_parts.append(best_text)

    full = "\n\n=== PAGE BREAK ===\n\n".join(text_parts)
    if cache_file:
        cache_file.write_text(full, encoding="utf-8")
    return full


def parse_ocr_table(ocr_text: str) -> dict:
    """Wyciąg listy imienne z surowego OCR.

    Heurystyka: na każdą linię szukamy wzorca "{Nachname}, {Vorname}
    [{fakcja}] {decyzja}" gdzie decyzja to Ja/Nein/Enthaltung/...

    OCR jest niedoskonały, więc tolerujemy:
    - przecinek może zniknąć: "Mustermann Max CDU Ja"
    - fakcja może być pomylona ("ODU" -> CDU): regex z KNOWN_CLUBS
    - decyzja na końcu linii
    """
    named_votes: dict[str, list[str]] = {
        "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []
    }
    councilor_clubs: dict[str, str] = {}

    # Pattern: nazwisko + opcjonalnie przecinek + imię + fakcja + decyzja
    # Tolerujemy litery OCR-trafficked z mieszanką cyfr/specjalnych w środku
    for raw_line in ocr_text.splitlines():
        line = raw_line.strip()
        if len(line) < 8:
            continue

        # Znajdź decyzję na końcu linii
        decision_cat = None
        decision_idx = -1
        for token, cat in DECISION_TOKENS.items():
            idx = line.rfind(token)
            if idx > decision_idx and idx > len(line) - 25:
                decision_idx = idx
                decision_cat = cat
        if not decision_cat:
            continue

        # Wszystko przed decyzją
        head = line[:decision_idx].rstrip(" :|.-")

        # Znajdź fakcję (rozpoznane z KNOWN_CLUBS)
        fraktion = "?"
        rest = head
        for club in sorted(KNOWN_CLUBS, key=len, reverse=True):
            club_idx = head.rfind(club)
            if club_idx >= 0:
                fraktion = club
                rest = head[:club_idx].rstrip(" ,|.")
                break

        # Imię i nazwisko: "Nachname, Vorname" lub "Nachname Vorname"
        if "," in rest:
            parts = rest.split(",", 1)
            nachname = parts[0].strip()
            vorname = parts[1].strip()
        else:
            words = rest.split()
            if len(words) < 2:
                continue
            nachname = words[0]
            vorname = " ".join(words[1:])

        # Filter: imię musi mieć litery, nie być za długie
        if not nachname or not vorname or len(nachname) > 30 or len(vorname) > 30:
            continue
        if not any(c.isalpha() for c in nachname):
            continue

        name = f"{vorname} {nachname}".strip()
        named_votes[decision_cat].append(name)
        if fraktion != "?":
            councilor_clubs[name] = fraktion

    return {"named_votes": named_votes, "councilor_clubs": councilor_clubs}


def build_kadencja(cache_dir: Path | None, limit_sessions: int | None = None) -> dict:
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    print("Pobieram listę sesji Stadtvertretung...", file=sys.stderr)
    sessions = list_sessions()
    print(f"  {len(sessions)} sesji od {KADENCJA_START.isoformat()}", file=sys.stderr)
    if limit_sessions:
        sessions = sessions[:limit_sessions]

    votes: list[dict] = []
    all_councilors: dict[str, str] = {}
    sess_meta: dict[str, dict] = {}

    for s in sessions:
        ksinr = s["ksinr"]
        try:
            anlagen = find_namentliche_anlagen(ksinr, cache_dir)
        except Exception as e:
            print(f"  WARN sesja {ksinr}: {e}", file=sys.stderr)
            continue

        if not anlagen:
            sess_meta[ksinr] = {**s, "vote_count": 0, "attendees": set()}
            continue

        print(f"  Sesja {s['date']} (ksinr={ksinr}): {len(anlagen)} namentliche", file=sys.stderr)
        sess_meta[ksinr] = {**s, "vote_count": 0, "attendees": set()}

        for an in anlagen:
            file_id = an["file_id"]
            url = FILE_TPL.format(file_id=file_id)
            try:
                pdf_bytes = fetch_bytes(url)
            except Exception as e:
                print(f"    ERR pobierania file_id={file_id}: {e}", file=sys.stderr)
                continue

            try:
                ocr_text = ocr_anlage_pdf(pdf_bytes, cache_dir, file_id)
            except Exception as e:
                print(f"    ERR OCR file_id={file_id}: {e}", file=sys.stderr)
                continue

            parsed = parse_ocr_table(ocr_text)
            named = parsed["named_votes"]
            counts = {cat: len(named[cat]) for cat in named}

            if sum(counts.values()) < 10:
                print(f"    WARN file_id={file_id}: tylko {sum(counts.values())} imion w OCR, podejrzane", file=sys.stderr)
                continue

            vote_id = f"{s['date']}_TOP{an['top'] or file_id}"
            votes.append({
                "id": vote_id,
                "source_url": url,
                "session_date": s["date"],
                "session_number": ksinr,
                "topic": an["label"],
                "druk": None,
                "resolution": None,
                "counts": counts,
                "named_votes": named,
                "passed": counts.get("za", 0) > counts.get("przeciw", 0),
            })

            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                sess_meta[ksinr]["attendees"].update(named[cat])
            sess_meta[ksinr]["vote_count"] += 1

            for name, fraktion in parsed["councilor_clubs"].items():
                all_councilors[name] = fraktion

            time.sleep(0.5)

    print(f"\nSparsowano {len(votes)} głosowań, {len(all_councilors)} posłów", file=sys.stderr)

    councilors = _build_councilors(votes, all_councilors)
    sessions_list = []
    for ksinr, m in sorted(sess_meta.items(), key=lambda x: x[1]["date"]):
        sessions_list.append({
            "date": m["date"],
            "number": ksinr,
            "vote_count": m["vote_count"],
            "attendee_count": len(m["attendees"]),
            "attendees": sorted(m["attendees"]),
        })

    club_counts: dict[str, int] = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    return {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "clubs": {club: cnt for club, cnt in sorted(club_counts.items())},
        "sessions": sessions_list,
        "total_sessions": len(sessions_list),
        "total_votes": len(votes),
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": votes,
        "similarity_top": [],
        "similarity_bottom": [],
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": BASE,
    }


def _build_councilors(votes: list[dict], club_map: dict[str, str]) -> list[dict]:
    """Same shape co Landtag MV. Bez similarity bo OCR niedokładny."""
    all_names = set()
    for v in votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    sessions_with_votes = set(v["session_date"] for v in votes)
    total_sessions = len(sessions_with_votes)
    total_votes = len(votes)

    cdata: dict[str, dict] = {n: {
        "name": n,
        "club": club_map.get(n, "?"),
        "votes_za": 0,
        "votes_przeciw": 0,
        "votes_wstrzymal": 0,
        "votes_brak": 0,
        "votes_nieobecny": 0,
        "sessions_present": set(),
    } for n in all_names}

    for v in votes:
        for cat, attr in [
            ("za", "votes_za"),
            ("przeciw", "votes_przeciw"),
            ("wstrzymal_sie", "votes_wstrzymal"),
            ("brak_glosu", "votes_brak"),
        ]:
            for name in v["named_votes"].get(cat, []):
                if name in cdata:
                    cdata[name][attr] += 1
                    cdata[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("nieobecni", []):
            if name in cdata:
                cdata[name]["votes_nieobecny"] += 1

    result = []
    for c in cdata.values():
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        result.append({
            "name": c["name"],
            "club": c["club"],
            "frekwencja": round(len(c["sessions_present"]) / total_sessions * 100, 1) if total_sessions else 0,
            "aktywnosc": round(present / total_votes * 100, 1) if total_votes else 0,
            "zgodnosc_z_klubem": 0,
            "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": 0,
            "rebellions": [],
            "has_activity_data": False,
            "activity": None,
        })
    return sorted(result, key=lambda c: c["name"])


def save_split_output(kadencja: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kid = kadencja["id"]
    kad_path = out_path.parent / f"kadencja-{kid}.json"
    with kad_path.open("w", encoding="utf-8") as f:
        json.dump(kadencja, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [{"id": kid, "label": kadencja["label"]}],
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Stadtvertretung Schwerin")
    parser.add_argument("--cache", type=Path, default=Path(".cache/schwerin"))
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data.json"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    print("=== Radoskop scraper: Stadtvertretung Schwerin (SessionNet + OCR) ===", file=sys.stderr)

    kadencja = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit)

    if kadencja["total_votes"] == 0 and (args.output.parent / f"kadencja-{KADENCJA_ID}.json").exists():
        print(f"\n✗ Zero głosowań, nie nadpisuję {args.output}", file=sys.stderr)
        return 1

    save_split_output(kadencja, args.output)

    print(f"\n✓ Zapisano {args.output}", file=sys.stderr)
    print(f"  Sesji: {kadencja['total_sessions']}", file=sys.stderr)
    print(f"  Głosowań: {kadencja['total_votes']}", file=sys.stderr)
    print(f"  Posłów: {kadencja['total_councilors']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
