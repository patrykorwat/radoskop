#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Lubuskiego, kadencja 2024-2029.

BIP lubuski (bip.lubuskie.pl) publikuje per-sesja PDF "Imienny wykaz głosowań
radnych" pod `/958/VII_kadencja__282024-2029_29/`. PDF jest SKANEM (brak
warstwy tekstowej), wymaga OCR z polskim packiem tesseract-ocr-pol.

Format (po OCR): 1 sesja = wiele głosowań, każde zaczyna się od "Wyniki
głosowania". Blok głosowania:
  Wyniki głosowania
  Sesja: ... / Punkt obrad: ... / Nazwa głosowania: {topic}
  Data głosowania: DD.MM.YYYY HH:MM
  Za: N  Przeciw: N  Wstrzymało się: N  Nieobecni: N  Uprawnionych: N
  Lp. Imię i nazwisko Głos Data i czas oddania głosu
  1 Imię Nazwisko Za DD.MM.YYYY HH:MM
  ... (tabela JEDNOkolumnowa, 1 wiersz = 1 radny)

NIE używamy `parse_voting_pdf_per_page` (to parser DWUkolumnowy kuj-pom).
Lubuski jest jednokolumnowy z odrębnym nagłówkiem zbiorczym.

Model dokładności: liczby zbiorcze z nagłówka (Za/Przeciw/Wstrzymało) OCR-ują
się czysto i są AUTORYTATYWNE (pole `counts`). Imienna atrybucja (`named_votes`)
pochodzi z OCR tabeli i bywa o 1-3 wiersze krótsza (skan gubi pojedyncze
linie); nazwiska korygujemy do rosteru przez dopasowanie rozmyte.

Output: schemat indeksowy (councilor_index + top-level votes), identyczny z
assemblies/dolnoslaskie i zachodniopomorskie, czytany przez build_assembly_metrics.py.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import ssl
import sys
import time
from collections import Counter
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://bip.lubuskie.pl"
INDEX_URL = f"{BASE}/958/VII_kadencja__282024-2029_29/"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 120
SLEEP_BETWEEN = 0.1
OCR_DPI = int(os.environ.get("LUBUSKIE_OCR_DPI", "300"))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

ROMAN_TO_ARABIC = {r: i for i, r in enumerate(
    ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII",
     "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII",
     "XXIII", "XXIV", "XXV", "XXVI", "XXVII", "XXVIII", "XXIX", "XXX"], start=1)}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str, *, cache_dir: Path | None = None, suffix: str = ".bin") -> bytes:
    cache_path = None
    if cache_dir:
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT, context=SSL_CTX) as resp:
            data = resp.read()
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GET {url} failed: {e}") from e
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    time.sleep(SLEEP_BETWEEN)
    return data


def fetch_html(url: str, *, cache_dir: Path | None = None) -> str:
    return fetch(url, cache_dir=cache_dir, suffix=".html").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_session_pdfs(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    body = fetch_html(INDEX_URL, cache_dir=cache_dir)
    pattern = re.compile(
        r'href="([^"]*?pobierz\.php\?plik=([IVXLCDM]+)_sesja_Sejmiku[_\s]*'
        r'(\d{2})\.(\d{2})\.(\d{4})[^"]*?[Ii]mienny[^"]*?)"',
        re.IGNORECASE,
    )
    sessions, seen = [], set()
    for href, roman, dd, mm, yyyy in pattern.findall(body):
        if href in seen:
            continue
        seen.add(href)
        pdf_url = (href if href.startswith("http") else BASE + href).replace("&amp;", "&")
        sessions.append({
            "session_number": roman.upper(),
            "date_iso": f"{yyyy}-{int(mm):02d}-{int(dd):02d}",
            "pdf_url": pdf_url,
        })
    return sessions


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def ocr_pdf(pdf_path: Path, cache_key: str, cache_dir: Path | None) -> list[str]:
    """OCR PDF do listy tekstów stron.

    Cache wznawialny PER STRONA: każda strona zapisywana osobno do
    {key}.ocr.pages/page_NNNN.txt zaraz po OCR, a strony już zcache'owane są
    pomijane przy kolejnym przebiegu. Dzięki temu ubicie procesu na timeoucie
    nie traci postępu — następny run dolicza tylko brakujące strony, więc nawet
    pojedyncza duża sesja (kilkadziesiąt skanów) domyka się przez kilka runów
    zamiast restartować od zera. Po komplecie scalamy do {key}.ocr.json
    (jednoplikowy szybki odczyt przy następnych runach).

    Strony renderujemy pojedynczo (first/last_page), żeby restart nie
    rasteryzował ponownie już zrobionych stron; gdy pdfinfo niedostępne,
    fallback rasteryzuje cały PDF naraz, ale wciąż cache'uje per strona.
    """
    if cache_dir:
        ocr_cache = cache_dir / f"{cache_key}.ocr.json"
        if ocr_cache.is_file():
            return json.loads(ocr_cache.read_text(encoding="utf-8"))
    from pdf2image import convert_from_path
    import pytesseract
    lang = "pol"
    try:
        if "pol" not in set(pytesseract.get_languages(config="")):
            lang = "eng"
    except Exception:
        lang = "eng"

    page_dir = (cache_dir / f"{cache_key}.ocr.pages") if cache_dir else None
    if page_dir:
        page_dir.mkdir(parents=True, exist_ok=True)

    def _page_path(i: int) -> Path | None:
        return (page_dir / f"page_{i:04d}.txt") if page_dir else None

    def _ocr_one(image, i: int, total: int | None) -> str:
        pf = _page_path(i)
        if pf and pf.is_file():
            return pf.read_text(encoding="utf-8")
        txt = pytesseract.image_to_string(image, lang=lang, config="--psm 4")
        if pf:
            pf.write_text(txt, encoding="utf-8")
        suffix = f"/{total}" if total else ""
        print(f"   OCR strona {i + 1}{suffix}", file=sys.stderr)
        return txt

    n_pages: int | None = None
    try:
        from pdf2image import pdfinfo_from_path
        n_pages = int(pdfinfo_from_path(str(pdf_path))["Pages"])
    except Exception:
        n_pages = None

    texts: list[str] = []
    if n_pages is not None:
        for i in range(n_pages):
            pf = _page_path(i)
            if pf and pf.is_file():
                texts.append(pf.read_text(encoding="utf-8"))
                continue
            imgs = convert_from_path(str(pdf_path), dpi=OCR_DPI,
                                     first_page=i + 1, last_page=i + 1)
            texts.append(_ocr_one(imgs[0], i, n_pages) if imgs else "")
    else:
        images = convert_from_path(str(pdf_path), dpi=OCR_DPI)
        for i, im in enumerate(images):
            texts.append(_ocr_one(im, i, len(images)))

    if cache_dir:
        (cache_dir / f"{cache_key}.ocr.json").write_text(
            json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return texts


# ---------------------------------------------------------------------------
# Parsowanie OCR (format jednokolumnowy lubuski)
# ---------------------------------------------------------------------------

_DEC = r'(Zza|ZA|Za|za|Przeciw|przeciw|Wstrzyma\S*\s*si\S*|Nieobecn\w*)'
# Wiersz tabeli imiennej: nazwisko (od wielkiej litery) + decyzja. Znacznik
# czasu jest OPCJONALNY — skan często gubi go w OCR, a wiersze "Nieobecny"
# nie mają go w ogóle. Wcześniejsza wersja wymagała "DD.MM.YYYY" po decyzji,
# co zaniżało recall atrybucji do ~60% na dpi150 (np. 202/416 głosów radnego).
# Sklejamy whitespace bloku i czytamy pary nazwisko+decyzja → ~93%.
_TS = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}\s*\d{0,2}:?\d{0,2}")
_NAME_DEC = re.compile(r'([A-ZĄĆĘŁŃÓŚŹŻ][^\d]{2,45}?)\s+' + _DEC + r'\b')


def _clean_name(name: str) -> str:
    """Czyści nazwisko po OCR: zostawia tylko tokeny od wielkiej litery,
    obcina śmieci ('mm', 'sz', '|', '.') wcięte między nazwisko a decyzję."""
    name = re.sub(r"[\s\|\-—_.:,;\"']+", " ", name).strip()
    toks = name.split()
    while toks and (len(toks[-1]) <= 1 or not toks[-1][:1].isupper()):
        toks.pop()
    while toks and (len(toks[0]) <= 1 or not toks[0][:1].isupper()):
        toks.pop(0)
    return " ".join(toks)


def _deckey(tok: str) -> str | None:
    t = tok.lower().replace(" ", "")
    if t.startswith("wstrzyma"):
        return "wstrzymal_sie"
    if t.startswith("nieobecn"):
        return "nieobecni"
    if t in ("za", "zza"):
        return "za"
    if t.startswith("przeciw"):
        return "przeciw"
    return None


def parse_session_votes(page_texts: list[str]) -> list[dict[str, Any]]:
    """Zwraca listę głosowań z sesji: counts (z nagłówka, autorytatywne) +
    surowe named_votes (nazwiska z OCR, do późniejszej korekty rosterem)."""
    full = "\n".join(page_texts)
    blocks = re.split(r"Wyniki g[łl]osowania", full)[1:]
    votes = []
    for b in blocks:
        counts = {}
        for key, pat in (("za", r"Za:\s*(\d+)"),
                         ("przeciw", r"Przeciw:\s*(\d+)"),
                         ("wstrzymal_sie", r"Wstrzyma\S*\s*si\S*:\s*(\d+)"),
                         ("nieobecni", r"Nieobecni:\s*(\d+)")):
            m = re.search(pat, b)
            counts[key] = int(m.group(1)) if m else 0
        counts["brak_glosu"] = 0
        m = re.search(r"Uprawnionych:\s*(\d+)", b)
        uprawnionych = int(m.group(1)) if m else None
        if counts["za"] == 0 and counts["przeciw"] == 0 and counts["wstrzymal_sie"] == 0 \
                and uprawnionych is None:
            continue  # nie wygląda na blok głosowania

        m = re.search(r"Nazwa g[łl]osowania:\s*(.+?)(?:Data g[łl]osowania|Miejsce:)", b, re.S)
        topic = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        m = re.search(r"Data g[łl]osowania:\s*(\d{2})\.(\d{2})\.(\d{4})\s*(\d{1,2}:\d{2})?", b)
        voted_at = None
        if m:
            voted_at = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" + (f" {m.group(4)}" if m.group(4) else "")

        # Tabela imienna jest w sekcji "podsumowanie szczegółowe"; sekcja
        # "zbiorcze" wyżej ma "Za: N" itp. (podsumowania, nie wiersze).
        # Sklejamy whitespace (skan łamie wiersze i wcina śmieci OCR między
        # nazwisko a decyzję), wycinamy znaczniki czasu, czytamy pary.
        i = b.lower().find("szczegó")
        body = b[i:] if i >= 0 else b
        flat = re.sub(r"\s+", " ", _TS.sub(" ", body))
        raw = []  # (name, decision_key)
        for nm, dec in _NAME_DEC.findall(flat):
            k = _deckey(dec)
            name = _clean_name(nm)
            if k and 2 <= len(name.split()) <= 3 and not any(c.isdigit() for c in name):
                raw.append((name, k))
        votes.append({
            "counts": counts,            # autorytatywne (nagłówek)
            "uprawnionych": uprawnionych,
            "topic": topic[:300],
            "voted_at": voted_at,
            "raw_named": raw,            # do korekty rosterem
        })
    return votes


SEED_ROSTER = [
    # KO (14)
    "Marcin Jabłoński", "Sebastian Ciemnoczołowski", "Anna Synowiec",
    "Grzegorz Potęga", "Hubert Harasimowicz", "Jerzy Wierchowicz",
    "Tomasz Gierczak", "Irena Osos", "Leszek Turczyniak", "Jarosław Dowhan",
    "Alicja Makarska", "Sławomir Kotylak", "Małgorzata Paluch-Słowińska",
    "Paweł Giza",
    # PiS (10)
    "Elżbieta Płonka", "Zofia Dajczak", "Bogusław Motowidełko",
    "Roman Jabłoński", "Jacek Kurzępa", "Tadeusz Ardelli", "Zbigniew Kościk",
    "Jakub Ast", "Małgorzata Gośniowska-Kola", "Andrzej Wieczorek",
    # TD (6)
    "Marek Halasz", "Zbigniew Kołodziej", "Edward Fedko", "Anna Chinalska",
    "Tomasz Siemiński", "Elżbieta Olga Polak",
]


def build_roster(all_sessions_votes: list[list[dict]], min_freq: int = 8) -> list[str]:
    """Seed oficjalnym składem (stabilizuje councilor_index = klucze
    config.club_assignments) + dorzucenie częstych nazwisk z OCR nie pasujących
    do seedu (np. zmiana mandatu w trakcie kadencji)."""
    roster = list(SEED_ROSTER)
    freq = Counter()
    for sv in all_sessions_votes:
        for v in sv:
            for nm, _ in v["raw_named"]:
                if 2 <= len(nm.split()) <= 3:
                    freq[nm] += 1
    for n, c in freq.most_common():
        if c >= min_freq and not difflib.get_close_matches(n, roster, n=1, cutoff=0.82):
            roster.append(n)
    return roster


def correct_named(raw: list[tuple[str, str]], roster: list[str]) -> dict[str, list[str]]:
    out = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
    seen = set()
    for nm, k in raw:
        m = difflib.get_close_matches(nm, roster, n=1, cutoff=0.8)
        canon = m[0] if m else None
        if canon and canon not in seen:
            seen.add(canon)
            out[k].append(canon)
    return out


# ---------------------------------------------------------------------------
# Schemat indeksowy (jak dolnoslaskie/zachodniopomorskie)
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            seen.update(names)
    s = sorted(seen)
    return s, {n: i for i, n in enumerate(s)}


def vote_to_indexed(v: dict, name_to_idx: dict[str, int]) -> dict:
    return {
        "id": f"{v['session_date']}_{v['vote_seq']}",
        "session_date": v["session_date"],
        "session_number": v["session_number"],
        "source_url": v["source_url"],
        "topic": v["topic"] or "Głosowanie",
        "druk": None,
        "resolution": None,
        "counts": v["counts"],
        "named_votes": {cat: sorted(name_to_idx[n] for n in names if n in name_to_idx)
                        for cat, names in v["named_votes"].items()},
        "voted_at": v.get("voted_at"),
    }


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        sess = by_date.setdefault(d, {
            "date": d, "number": v["session_number"], "vote_count": 0,
            "attendees": set(), "attendee_count": 0, "speakers": [],
        })
        sess["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sess["attendees"].update(v["named_votes"].get(cat, []))
    out = []
    for d in sorted(by_date, reverse=True):
        s = by_date[d]
        s["attendees"] = sorted(s["attendees"])
        s["attendee_count"] = len(s["attendees"])
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_kadencja(cache_dir: Path | None = None,
                   limit_sessions: int | None = None,
                   output_path: Path | None = None) -> dict[str, Any]:
    print("==> Discovering session PDFs...", file=sys.stderr)
    sessions = discover_session_pdfs(cache_dir=cache_dir)
    print(f"==> Found {len(sessions)} sesji", file=sys.stderr)
    if limit_sessions:
        sessions = sessions[:limit_sessions]

    def _assemble(parsed: list) -> dict[str, Any]:
        # Roster z dotychczas zparsowanych sesji (bootstrap z OCR) + korekta.
        roster = build_roster([v for _, v in parsed])
        all_votes = []
        mism = 0
        for sess, votes in parsed:
            for seq, v in enumerate(votes):
                named = correct_named(v["raw_named"], roster)
                # liczba przypisanych aktywnych vs nagłówek (jakość OCR)
                for cat in ("za", "przeciw", "wstrzymal_sie"):
                    if len(named[cat]) != v["counts"][cat]:
                        mism += 1
                        break
                all_votes.append({
                    "session_date": sess["date_iso"],
                    "session_number": sess["session_number"],
                    "vote_seq": seq,
                    "source_url": sess["pdf_url"],
                    "topic": v["topic"],
                    "voted_at": v["voted_at"],
                    "counts": v["counts"],          # nagłówek = autorytatywne
                    "named_votes": named,           # atrybucja OCR (best-effort)
                })
        councilors, name_to_idx = build_councilor_index(all_votes)
        indexed = [vote_to_indexed(v, name_to_idx) for v in all_votes]
        sessions_agg = aggregate_sessions(all_votes)
        return {
            "id": KADENCJA_ID,
            "label": KADENCJA_LABEL,
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sessions": sessions_agg,
            "total_sessions": len(sessions_agg),
            "total_votes": len(indexed),
            "total_councilors": len(councilors),
            "councilors": [],
            "votes": indexed,
            "similarity_top": [],
            "similarity_bottom": [],
            "councilor_index": councilors,
            "ocr_attribution_mismatches": mism,
            "source": INDEX_URL,
        }

    def _flush(parsed: list) -> None:
        # Zapis przyrostowy po KAŻDEJ sesji: ubicie na timeoucie nie traci sesji
        # już zOCR-owanych, a jedna gruba sesja nie blokuje publikacji reszty.
        # Nie nadpisujemy dobrego pliku zerami (guard total_votes). Atomowo.
        if output_path is None or not parsed:
            return
        kad = _assemble(parsed)
        if kad["total_votes"] == 0:
            return
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_text(json.dumps(kad, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(output_path)

    parsed_sessions = []  # (meta, [votes])
    for sess in sessions:
        print(f"=> Sesja {sess['session_number']} ({sess['date_iso']}) OCR...", file=sys.stderr)
        try:
            pdf_bytes = fetch(sess["pdf_url"], cache_dir=cache_dir, suffix=".pdf")
        except Exception as e:
            print(f"   WARN download: {e}", file=sys.stderr)
            continue
        key = md5(sess["pdf_url"].encode()).hexdigest()
        tmp = (cache_dir or Path("/tmp")) / f"{key}.pdf"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(pdf_bytes)
        try:
            page_texts = ocr_pdf(tmp, key, cache_dir)
            votes = parse_session_votes(page_texts)
        except Exception as e:
            print(f"   WARN OCR/parse: {e}", file=sys.stderr)
            continue
        print(f"   {len(votes)} głosowań", file=sys.stderr)
        parsed_sessions.append((sess, votes))
        _flush(parsed_sessions)

    return _assemble(parsed_sessions)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape Sejmik Województwa Lubuskiego (skan+OCR)")
    ap.add_argument("--cache", type=Path, default=Path(".cache/lubuskie"))
    ap.add_argument("--output", "-o", type=Path, default=Path("docs/kadencja-2024-2029.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    kad = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit,
                         output_path=args.output)
    if kad["total_votes"] == 0 and args.output.exists():
        print("\n✗ Zero głosowań — pomijam zapis (zostaje poprzednia wersja).", file=sys.stderr)
        print("  Sprawdź tesseract-ocr-pol + pdf2image na NAS.", file=sys.stderr)
        return 1
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(kad, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Saved {args.output}: {kad['total_sessions']} sesji / "
          f"{kad['total_votes']} głosowań / {kad['total_councilors']} radnych / "
          f"OCR-mismatch {kad['ocr_attribution_mismatches']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
