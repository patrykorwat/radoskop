#!/usr/bin/env python3
"""Scraper głosowań Sejmiku Województwa Zachodniopomorskiego, kadencja 2024-2029.

Źródło: BIP wzp.pl (Drupal). Strona "Wyniki głosowań / Kadencja 2024-2029"
(https://bip.wzp.pl/tabela/artykuly/771/2330) listuje artykuły sesji. Każdy
artykuł sesji zawiera listę podjętych uchwał (numer + opis) oraz załączniki
PDF: 1 PDF = 1 imienne głosowanie. PDF ma cyfrową warstwę tekstową (NIE skan,
więc bez OCR), w formacie eSesja "wydruk": dwukolumnowa tabela
"Lp. | Nazwisko i imię | Głos".

Nazwa pliku PDF koduje numer uchwały: `xvii.224.26_1.pdf` -> "XVII/224/26",
co pozwala dopiąć czysty opis tematu z listy uchwał w artykule.

Parser współrzędnościowy (parse_page) grupuje słowa po pozycji X (dwie kolumny)
i przypisuje fragmenty nazwisk do najbliższego wiersza "Głos" po osi Y, co jest
odporne na zawijanie długich nazwisk na kilka linii.

Output: docs/kadencja-2024-2029.json zgodny ze schemą innych sejmików
(sessions[].votes[].named_votes{za,przeciw,wstrzymal_sie,brak_glosu,nieobecni}).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://bip.wzp.pl"
INDEX_URL = f"{BASE}/tabela/artykuly/771/2330"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "VII kadencja (2024–2029)"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 60
SLEEP_BETWEEN = 0.15

# Reuse istniejącego parsera eSesja "standard" (Głosowano w sprawie / Wyniki
# imienne) dla sesji wyeksportowanych w innym formacie niż dwukolumnowy wydruk.
# assemblies/{slug}/scripts/scrape_glosowania.py -> radoskop/scripts
_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
try:
    from lib_voting_pdf_table import extract_pdf_text as _lib_extract_text
    from lib_voting_pdf_table import parse_voting_text as _lib_parse_text
except Exception:  # pragma: no cover
    _lib_extract_text = None
    _lib_parse_text = None

# Wariant stopki eSesja w PDF-ach wzp.pl ("Głosowanie zakończono w dniu: ...
# Wygenerowano w systemie eSesja.pl"), którego współdzielona lib nie obcina,
# przez co skleja się z ostatnim nazwiskiem w sekcji. Usuwamy go lokalnie.
_WZP_FOOTER_RE = re.compile(r"Głosowanie zakończono w dniu:.*", re.S)


ROMAN_TO_ARABIC = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
    "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
    "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
    "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
    "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32, "XXXIII": 33,
    "XXXIV": 34, "XXXV": 35, "XXXVI": 36, "XXXVII": 37, "XXXVIII": 38,
    "XXXIX": 39, "XL": 40,
}
POLISH_MONTHS = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "wrzesnia": "09", "października": "10", "pazdziernika": "10",
    "listopada": "11", "grudnia": "12",
}


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
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
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


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&quot;", '"').replace("&#039;", "'")
            .replace("&bdquo;", "„").replace("&rdquo;", "”")
            .replace("&ndash;", "–").replace("&oacute;", "ó"))
    return re.sub(r"\s+", " ", html)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

SESSION_LINK_RE = re.compile(
    r'href="(/artykul/([ivxlcdm]+)-sesja-sejmiku-wojewodztwa-zachodniopomorskiego[^"]*)"',
    re.IGNORECASE,
)


def discover_sessions(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Lista artykułów sesji z indeksu (z paginacją ?page=N)."""
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 0
    while True:
        url = INDEX_URL if page == 0 else f"{INDEX_URL}?page={page}"
        body = fetch_html(url, cache_dir=cache_dir)
        new = 0
        for href, roman_lower in SESSION_LINK_RE.findall(body):
            if href in seen:
                continue
            seen.add(href)
            new += 1
            sessions.append({
                "url": BASE + href,
                "session_number": roman_lower.upper(),
            })
        if new == 0:
            break
        page += 1
        if page > 20:  # bezpiecznik
            break
    return sessions


def parse_session_article(url: str, cache_dir: Path | None = None) -> dict[str, Any]:
    """Wyciąga z artykułu sesji: datę, numer rzymski, mapę uchwał i listę PDF."""
    body = fetch_html(url, cache_dir=cache_dir)

    # Tytuł: "XVII Sesja Sejmiku Województwa Zachodniopomorskiego | 26 maja 2026 r."
    title_m = re.search(r"<title>(.*?)</title>", body, re.S)
    title = title_m.group(1).strip() if title_m else ""

    roman_m = re.match(r"\s*([IVXLCDM]+)\s+Sesja", title, re.IGNORECASE)
    roman = roman_m.group(1).upper() if roman_m else None

    # Data z tytułu: "26 maja 2026"
    date_iso = None
    month_pat = "|".join(POLISH_MONTHS)
    dm = re.search(rf"(\d{{1,2}})\s+({month_pat})\s+(\d{{4}})", title, re.IGNORECASE)
    if dm:
        d, mname, y = dm.groups()
        date_iso = f"{y}-{POLISH_MONTHS[mname.lower()]}-{int(d):02d}"

    # Mapa uchwał: numer -> opis (z treści artykułu, po strip tagów)
    text = strip_tags(body)
    reso_map: dict[str, str] = {}
    # Wzorzec: "XVII/224/26 w sprawie ... " do następnego numeru uchwały
    reso_iter = list(re.finditer(r"([IVXLCDM]+/\d+/\d+)\s+(.*?)(?=[IVXLCDM]+/\d+/\d+|$)", text))
    for m in reso_iter:
        num = m.group(1)
        desc = m.group(2).strip()
        # Utnij ogony nawigacyjne / numerację listy
        desc = re.split(r"\s+\d+\.\s+[IVXLCDM]+/", desc)[0].strip()
        desc = desc.rstrip(" .;")
        if num not in reso_map and 5 < len(desc) < 600:
            reso_map[num] = desc

    # Załączniki PDF (pełne URL-e)
    pdfs = []
    for href in re.findall(r'href="([^"]+\.pdf)"', body):
        if href not in pdfs:
            pdfs.append(href if href.startswith("http") else BASE + href)

    return {
        "url": url,
        "title": title,
        "number_roman": roman,
        "number": ROMAN_TO_ARABIC.get(roman) if roman else None,
        "date": date_iso,
        "reso_map": reso_map,
        "pdfs": pdfs,
    }


def resolution_from_pdf_url(pdf_url: str) -> str | None:
    """`.../xvii.224.26_1.pdf` -> 'XVII/224/26'."""
    name = pdf_url.rsplit("/", 1)[-1]
    m = re.match(r"([ivxlcdm]+)\.(\d+)\.+(\d+)", name, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1).upper()}/{int(m.group(2))}/{m.group(3)}"


# ---------------------------------------------------------------------------
# PDF parsing (coordinate-aware, eSesja text-layer print)
# ---------------------------------------------------------------------------

MAX_NAME_DIST = 12.0  # px: name token musi być blisko swojego wiersza Głos

# Dokładne tokeny kolumny "Głos" (eSesja). Dopasowanie po RÓWNOŚCI, nie po
# podciągu — inaczej "ZA" trafia w nazwiska (Fedeń-cza-k, Kur-za-wa,
# Małgo-rza-ta). Tokeny kontynuacyjne (SIĘ, GŁOSU, GŁOSOWAŁ) też tu są, żeby
# nie wpadły do nazwisk; o kategorii decyduje pierwszy token wiersza.
GLOS_EXACT = {
    "ZA", "PRZECIW",
    "WSTRZYMUJĘ", "WSTRZYMUJE", "WSTRZYMAŁ", "WSTRZYMAL", "WSTRZYMAŁA", "WSTRZYMALA",
    "SIĘ", "SIE",
    "NIEOBECNY", "NIEOBECNA", "NIEOBECNI",
    "OBECNY", "OBECNA",
    "BRAK", "GŁOSU", "GLOSU", "NIE", "GŁOSOWAŁ", "GLOSOWAL", "GŁOSOWAŁA",
    "NIEGŁOSUJĄCY",
}


def _is_glos(text: str) -> bool:
    return text.upper().strip() in GLOS_EXACT


def _decision_key(text: str) -> str | None:
    tokens = text.upper().split()
    if not tokens:
        return None
    first = tokens[0]
    if first.startswith("NIEOBECN"):
        return "nieobecni"
    if first.startswith("WSTRZYM"):
        return "wstrzymal_sie"
    if first == "PRZECIW":
        return "przeciw"
    if first == "ZA":
        return "za"
    if first in ("OBECNY", "OBECNA", "BRAK", "NIE", "NIEGŁOSUJĄCY"):
        return "brak_glosu"  # obecny, ale nie oddał głosu
    return None


def _cluster_by_top(words, tol=3.0):
    words = sorted(words, key=lambda w: w["top"])
    rows = []
    for w in words:
        if rows and abs(w["top"] - rows[-1]["top"]) <= tol:
            rows[-1]["items"].append(w)
            rows[-1]["top"] = (rows[-1]["top"] + w["top"]) / 2
        else:
            rows.append({"top": w["top"], "items": [w]})
    return rows


def parse_vote_page(page) -> dict[str, Any] | None:
    words = page.extract_words()
    if not words:
        return None
    header = page.extract_text() or ""

    counts_hdr: dict[str, int] = {}
    for key, pat in (("za", r"Głosy za\s+(\d+)"),
                     ("przeciw", r"Głosy przeciw\s+(\d+)"),
                     ("wstrzymal_sie", r"Głosy wstrzymując\w*\s+się\s+(\d+)"),
                     ("nieobecni", r"Liczba nieobecnych\s+(\d+)"),
                     ("brak_glosu", r"Obecni niegłosując\w*\s+(\d+)")):
        m = re.search(pat, header)
        if m:
            counts_hdr[key] = int(m.group(1))
    m = re.search(r"Liczba uprawnionych\s+(\d+)", header)
    uprawnionych = int(m.group(1)) if m else None

    voted_at = None
    m = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})\s*(\d{1,2}:\d{2})?", header)
    if m:
        d, mo, y, tm = m.groups()
        voted_at = f"{y}-{mo}-{d}" + (f" {tm}" if tm else "")

    hdr_top = next((w["top"] for w in words if w["text"] == "Nazwisko"), None)
    table_top = (hdr_top + 8) if hdr_top else 320

    named: dict[str, list[str]] = {
        "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []
    }

    for half in ("L", "R"):
        if half == "L":
            lp_max, name_lo, name_hi = 56, 56, 205
        else:
            lp_max, name_lo, name_hi = 320, 320, 470
        col = [w for w in words if w["top"] > table_top
               and ((w["x0"] < 290) == (half == "L"))]
        glos_words, name_words = [], []
        for w in col:
            t = w["text"]
            if w["x0"] < lp_max and re.match(r"^\d+\.?$", t):
                continue  # Lp marker, pomijamy
            # "Głos" rozpoznajemy po słowniku (ZA/PRZECIW/WSTRZYMUJĘ/...),
            # nie po X — dłuższe "WSTRZYMUJĘ SIĘ" jest wyrównane w lewo
            # i wchodziłoby w pas nazwisk.
            if _is_glos(t):
                glos_words.append(w)
            elif name_lo <= w["x0"] < name_hi:
                name_words.append(w)
        glos_rows = _cluster_by_top(glos_words)
        for g in glos_rows:
            g["text"] = " ".join(x["text"] for x in sorted(g["items"], key=lambda x: x["x0"]))
            g["names"] = []
        if not glos_rows:
            continue
        for nw in name_words:
            best = min(glos_rows, key=lambda g: abs(g["top"] - nw["top"]))
            if abs(best["top"] - nw["top"]) <= MAX_NAME_DIST:
                best["names"].append(nw)
        for g in glos_rows:
            key = _decision_key(g["text"])
            if not key:
                continue
            nm = " ".join(x["text"] for x in sorted(g["names"], key=lambda x: (x["top"], x["x0"])))
            nm = re.sub(r"\s*-\s*", "-", nm).strip()
            nm = re.sub(r"\s+", " ", nm)
            if nm:
                named[key].append(nm)

    counts = {k: len(v) for k, v in named.items()}
    if sum(counts.values()) < 5:
        return None
    return {
        "named_votes": named,
        "counts": counts,
        "counts_header": counts_hdr,
        "uprawnionych": uprawnionych,
        "voted_at": voted_at,
    }


def counts_match(parsed: dict[str, Any]) -> bool:
    h = parsed.get("counts_header") or {}
    c = parsed["counts"]
    for k in ("za", "przeciw", "wstrzymal_sie", "nieobecni"):  # brak_glosu poza tabelą
        if k in h and h[k] != c[k]:
            return False
    return True


# ---------------------------------------------------------------------------
# Parsowanie pojedynczego PDF (dyspozytor formatów)
# ---------------------------------------------------------------------------

CATS = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")


def parse_one_pdf(path: Path):
    """Zwraca (parsed_dict, format_str) albo (None, format_str).

    Formaty:
      "two_column"      — dwukolumnowy wydruk eSesja (parser współrzędnościowy,
                          nazwiska "Nazwisko Imię")
      "esesja_standard" — "Głosowano w sprawie / Wyniki imienne" (reuse lib;
                          nazwiska "Imię Nazwisko", do remapu)
      "scanned"         — skan bez warstwy tekstowej (pomijany)
    """
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        page0 = pdf.pages[0]
        sample = page0.extract_text() or ""
        if len(sample.strip()) < 50:
            return None, "scanned"
        if "Wyniki imienne" in sample:
            if _lib_parse_text is None:
                return None, "esesja_standard"
            full, first = _lib_extract_text(path)
            voted_at = None
            m = re.search(r"zakończono w dniu:\s*(\d{4}-\d{2}-\d{2})"
                          r"(?:,?\s*o godz\.\s*(\d{1,2}:\d{2}))?", full)
            if m:
                voted_at = m.group(1) + (f" {m.group(2)}" if m.group(2) else "")
            full = _WZP_FOOTER_RE.sub("", full)
            res = _lib_parse_text(full, first, source_name=path.name)
            if not res.get("votes"):
                return None, "esesja_standard"
            v = res["votes"][0]
            named = {k: list(v.get("named_votes", {}).get(k, [])) for k in CATS}
            counts = {k: len(named[k]) for k in CATS}
            hdr = v.get("counts") or {}
            return ({
                "named_votes": named,
                "counts": counts,
                "counts_header": {k: hdr.get(k, counts[k]) for k in CATS},
                "voted_at": v.get("voted_at") or voted_at,
            }, "esesja_standard")
        return parse_vote_page(page0), "two_column"


def _name_tokens(name: str) -> set[str]:
    return {t.lower() for t in name.split() if t}


def remap_to_canonical(name: str, canon_index: list[tuple[set[str], str]]) -> str:
    """Mapuje nazwisko "Imię Nazwisko" na kanoniczne "Nazwisko Imię" przez
    pokrycie tokenów (kolejność słów bez znaczenia). Wymaga ≥2 wspólnych
    tokenów; inaczej zwraca formę z nazwiskiem na początku (heurystyka:
    ostatni token to nazwisko)."""
    e = _name_tokens(name)
    best, best_ov = None, 0
    for toks, canon in canon_index:
        ov = len(e & toks)
        if ov > best_ov:
            best, best_ov = canon, ov
    if best is not None and best_ov >= 2:
        return best
    parts = name.split()
    return " ".join([parts[-1]] + parts[:-1]) if len(parts) >= 2 else name


# ---------------------------------------------------------------------------
# Składanie schematu sejmiku (councilor_index + indeksowane głosy + sesje)
# Identyczny kontrakt jak assemblies/dolnoslaskie (czytany przez
# build_assembly_metrics.py).
# ---------------------------------------------------------------------------

def build_councilor_index(votes: list[dict]) -> tuple[list[str], dict[str, int]]:
    seen: set[str] = set()
    for v in votes:
        for names in v["named_votes"].values():
            seen.update(names)
    sorted_names = sorted(seen)
    return sorted_names, {n: i for i, n in enumerate(sorted_names)}


def vote_to_indexed(vote: dict, name_to_idx: dict[str, int]) -> dict:
    vid = vote.get("resolution") or vote.get("voted_at") or "?"
    return {
        "id": f"{vote['session_date']}_{vid.replace('/', '_').replace(' ', '_')}",
        "session_date": vote["session_date"],
        "session_number": vote.get("session_number", ""),
        "source_url": vote["source_url"],
        "topic": vote["topic"],
        "druk": vote.get("druk") or None,
        "resolution": vote.get("resolution") or None,
        "counts": vote["counts"],
        "named_votes": {
            cat: sorted(name_to_idx[n] for n in names if n in name_to_idx)
            for cat, names in vote["named_votes"].items()
        },
        "voted_at": vote.get("voted_at"),
    }


def aggregate_sessions(votes: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for v in votes:
        d = v["session_date"]
        if not d:
            continue
        sess = by_date.setdefault(d, {
            "date": d,
            "number": v.get("session_number", ""),
            "vote_count": 0,
            "attendees": set(),
            "attendee_count": 0,
            "speakers": [],
        })
        sess["vote_count"] += 1
        # Obecny = oddał jakikolwiek głos lub był obecny niegłosujący (brak_glosu)
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
                   limit_sessions: int | None = None) -> dict[str, Any]:
    print("==> Discovering sessions...", file=sys.stderr)
    sessions_meta = discover_sessions(cache_dir=cache_dir)
    print(f"==> Found {len(sessions_meta)} sesji", file=sys.stderr)
    if limit_sessions:
        sessions_meta = sessions_meta[:limit_sessions]

    all_votes: list[dict[str, Any]] = []      # płaska lista, named_votes = nazwiska
    canon_names: set[str] = set()             # nazwiska z formatu dwukolumnowego
    pending_remap: list[dict[str, Any]] = []  # głosy esesja_standard do remapu
    mismatch = 0
    skipped_scanned = 0

    for sm in sessions_meta:
        art = parse_session_article(sm["url"], cache_dir=cache_dir)
        roman = art["number_roman"] or sm["session_number"]
        print(f"\n=> Sesja {roman} ({art['date']}) — {len(art['pdfs'])} PDF",
              file=sys.stderr)
        sess_votes = 0

        for pdf_url in art["pdfs"]:
            reso = resolution_from_pdf_url(pdf_url)
            try:
                pdf_bytes = fetch(pdf_url, cache_dir=cache_dir, suffix=".pdf")
            except Exception as e:
                print(f"   WARN download {pdf_url}: {e}", file=sys.stderr)
                continue
            tmp = (cache_dir or Path("/tmp")) / (md5(pdf_url.encode()).hexdigest() + ".pdf")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(pdf_bytes)
            try:
                parsed, fmt = parse_one_pdf(tmp)
            except Exception as e:
                print(f"   WARN parse {pdf_url}: {e}", file=sys.stderr)
                continue
            if fmt == "scanned":
                skipped_scanned += 1
                print(f"   ! skan bez warstwy tekstowej: {reso}", file=sys.stderr)
                continue
            if not parsed:
                continue
            if not counts_match(parsed):
                mismatch += 1
                print(f"   ! count mismatch {reso}: parsed={parsed['counts']} "
                      f"header={parsed['counts_header']}", file=sys.stderr)
            topic = art["reso_map"].get(reso) if reso else None
            vote = {
                "session_date": art["date"] or (parsed["voted_at"] or "")[:10],
                "session_number": roman,
                "source_url": pdf_url,
                "topic": topic or (f"Uchwała {reso}" if reso else "Głosowanie"),
                "druk": None,
                "resolution": reso,
                "counts": parsed["counts"],
                "named_votes": parsed["named_votes"],
                "voted_at": parsed["voted_at"],
            }
            all_votes.append(vote)
            sess_votes += 1
            if fmt == "two_column":
                for names in parsed["named_votes"].values():
                    canon_names.update(names)
            else:
                pending_remap.append(vote)

        if sess_votes == 0:
            print(f"   (brak sparsowanych głosowań)", file=sys.stderr)

    # Remap nazwisk z formatu esesja_standard ("Imię Nazwisko") na kanoniczne
    # "Nazwisko Imię" zebrane z formatu dwukolumnowego.
    if pending_remap:
        canon_index = [(_name_tokens(n), n) for n in canon_names]
        remapped = 0
        for vote in pending_remap:
            for cat in CATS:
                new = [remap_to_canonical(n, canon_index) for n in vote["named_votes"][cat]]
                remapped += sum(1 for a, b in zip(vote["named_votes"][cat], new) if a != b)
                vote["named_votes"][cat] = new
        print(f"\n==> Remapped {remapped} nazwisk esesja_standard -> kanoniczne",
              file=sys.stderr)

    councilors, name_to_idx = build_councilor_index(all_votes)
    indexed_votes = [vote_to_indexed(v, name_to_idx) for v in all_votes]
    sessions = aggregate_sessions(all_votes)

    return {
        "id": KADENCJA_ID,
        "label": KADENCJA_LABEL,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sessions": sessions,
        "total_sessions": len(sessions),
        "total_votes": len(indexed_votes),
        "total_councilors": len(councilors),
        "councilors": [],          # statystyki per radny — wypełnia build_assembly_metrics.py
        "votes": indexed_votes,
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilors,
        "count_mismatches": mismatch,
        "skipped_scanned": skipped_scanned,
        "source": INDEX_URL,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape Sejmik Województwa Zachodniopomorskiego")
    ap.add_argument("--cache", type=Path, default=Path(".cache/zachodniopomorskie"))
    ap.add_argument("--output", "-o", type=Path, default=Path("docs/kadencja-2024-2029.json"))
    ap.add_argument("--limit", type=int, default=None, help="Limit sesji (debug)")
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    kad = build_kadencja(cache_dir=args.cache, limit_sessions=args.limit)

    if kad["total_sessions"] == 0 and args.output.exists():
        print("\n✗ Zero sesji — pomijam zapis (zostaje poprzednia wersja)", file=sys.stderr)
        return 1

    with args.output.open("w", encoding="utf-8") as f:
        json.dump(kad, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Saved {args.output}", file=sys.stderr)
    print(f"  Sesji: {kad['total_sessions']}  Głosowań: {kad['total_votes']}  "
          f"Radnych: {kad['total_councilors']}  Mismatchy: {kad['count_mismatches']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
