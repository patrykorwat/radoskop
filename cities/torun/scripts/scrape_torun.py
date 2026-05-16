#!/usr/bin/env python3
"""
Radoskop Toruń — scraper sesji Rady Miasta Torunia.

Źródło danych: https://bip.torun.pl/ (BIP CMS firmy Logonet, 2.9.0).

Struktura:
  Indeks sesji: https://bip.torun.pl/sesje/0/{page}/25
    paginated list 25 per page, linki do per-session pages.
  Per-session: https://bip.torun.pl/sesja/{id}/{slug}
    sekcja "Załączniki" zawiera PDF "wyniki głosowań" pod
    https://bip.torun.pl/attachments/download/{att_id}

Wyniki głosowań to scanned PDF (obrazy CCITTFaxDecode, brak warstwy
tekstowej). Każda strona PDF = jedno głosowanie z 25 wierszami radnych.
Parser używa pdftoppm + tesseract -l pol --psm 6 do OCR. OCR text per
strona cachowany na dysku (klucz = sha1(url + page_idx)).

Etykiety głosów (z odporością na artefakty OCR):
  ZA  / ŻA                     → za
  PRZECIW                       → przeciw
  WSTRZYMUJĘ SIĘ                → wstrzymal_sie
  OBECNY / OBECNA               → brak_glosu (obecny niegłosujący)
  NIEOBECNY / NIEOBECNA         → nieobecni

Skład rady (25 radnych, IX kadencja 2024-2029) plus przypisania do klubów
znajduje się w config.json/club_assignments. Kanoniczne nazwiska służą
też jako anchor do dopasowania wierszy w mocno zaszumiony OCR (Cynk-
Mikołajewska, Czyżniewski itp.).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from lib_bip_static import BipScraper  # noqa: E402


BASE_URL = "https://bip.torun.pl"
SESSION_INDEX_TPL = f"{BASE_URL}/sesje/0/{{page}}/25"
MAX_INDEX_PAGES = 30  # twarda granica iteracji paginacji

POLISH_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

# "DD miesiąca YYYY r., N sesja ..." w tekście linku
SESSION_TITLE_RE = re.compile(
    r"(\d{1,2})\s+(\S+?)\s+(\d{4})\s*r\.\s*,\s*(\d+)(?:\s+(?:uroczysta\s+)?sesja)",
    re.UNICODE | re.IGNORECASE,
)

# Etykiety głosów: OCR często myli ZA↔ŻA i traci diakrytyki
LABEL_PATTERNS = [
    (re.compile(r"\bNIEOBECN[YAĄ]\b", re.IGNORECASE), "nieobecni"),
    (re.compile(r"\bOBECN[YAĄ]\b", re.IGNORECASE), "brak_glosu"),
    (re.compile(r"WSTRZYMUJ[ĘE]\s*SI[ĘE]", re.IGNORECASE), "wstrzymal_sie"),
    (re.compile(r"\bPRZE[CĆ]IW\b", re.IGNORECASE), "przeciw"),
    (re.compile(r"\b[ZŻ]A\b", re.IGNORECASE), "za"),
]


def parse_session_link_text(text: str) -> tuple[str, str] | None:
    """Parse '30 kwietnia 2026 r., 27 sesja Rady Miasta Torunia' → ('27', '2026-04-30')."""
    m = SESSION_TITLE_RE.search(text)
    if not m:
        return None
    day, month_name, year, number = m.groups()
    month = POLISH_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        date = f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None
    return number, date


def _normalize_for_match(s: str) -> str:
    """Lowercase + strip diakrytyki + OCR-typowe substytucje (l↔ł, o↔ó↔0,
    z↔ż↔ź, 6↔o, itp.). Cel: surname jako anchor mimo zaszumionego OCR.

    Nie wszystkie diakrytyki PL rozłożą się w NFD ('ł' i 'Ł' to osobne
    code-pointy, nie litera+diacritic), więc po NFD ręcznie mapujemy
    pozostałe i częste OCR confusions (J6żwiak zamiast Jóźwiak,
    Koziałocki zamiast Koziołocki itd.)."""
    nfd = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    stripped = stripped.lower()
    # PL-specific
    table = str.maketrans({
        "ł": "l", "Ł": "l",
        "ó": "o", "Ó": "o",
        # OCR często myli te:
        "0": "o",
        "6": "o",
        "1": "l",
        "!": "l",
        "ż": "z", "ź": "z",
    })
    return stripped.translate(table)


class TorunScraper(BipScraper):
    """Scraper BIP Toruń — sesje + głosowania imienne (OCR scanned PDF)."""

    def discover_sessions(self) -> list[dict]:
        """Paginate przez /sesje/0/{page}/25 i wyciągnij wszystkie linki do sesji.

        Pętlimy do napotkania pierwszej strony bez nowych sesji albo do
        MAX_INDEX_PAGES. Filtrowanie po kadencja_start robi już base class
        w run().
        """
        sessions: dict[str, dict] = {}
        for page in range(1, MAX_INDEX_PAGES + 1):
            url = SESSION_INDEX_TPL.format(page=page)
            try:
                soup = self.fetch(url)
            except Exception as exc:
                print(f"  Stop paginacji na stronie {page}: {exc}")
                break
            before = len(sessions)
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if not isinstance(href, str) or "/sesja/" not in href:
                    continue
                if "/sesja/pdf/" in href:
                    continue
                full_url = urljoin(BASE_URL, href.split("#")[0].split("?")[0])
                if full_url in sessions:
                    continue
                text = a.get_text(" ", strip=True)
                parsed = parse_session_link_text(text)
                if not parsed:
                    continue
                number, date = parsed
                sessions[full_url] = {
                    "url": full_url,
                    "date": date,
                    "number": number,
                    "title": text,
                }
            new = len(sessions) - before
            print(f"  Strona {page}: +{new} sesji (łącznie {len(sessions)})")
            if new == 0:
                break
        return sorted(sessions.values(), key=lambda s: s["date"])

    def parse_session_votes(self, session: dict) -> list[dict]:
        """Pobierz stronę sesji, znajdź PDF 'wyniki głosowań', OCR per strona,
        sparsuj każdą stronę jako jedno głosowanie.

        Sesje przyszłe (jeszcze nie odbyły się) nie mają załącznika z wynikami:
        zwracamy pustą listę, pipeline kontynuuje bez błędu.
        """
        try:
            soup = self.fetch(session["url"])
        except Exception as exc:
            print(f"    Blad fetchu strony sesji: {exc}")
            return []

        pdf_url = None
        for a in soup.select("a[href]"):
            label = a.get_text(" ", strip=True).lower()
            href = a.get("href") or ""
            if not isinstance(href, str):
                continue
            if "wynik" in label and "głosowa" in label and "/attachments/download/" in href:
                pdf_url = urljoin(BASE_URL, href)
                break
        if not pdf_url:
            print(f"    Brak załącznika 'wyniki głosowań' (sesja przyszła lub bez wyników).")
            return []

        try:
            pdf_bytes = self.fetch_bytes(pdf_url)
        except Exception as exc:
            print(f"    Blad pobierania PDF: {exc}")
            return []

        canonical_names = list(self.councilors.keys())
        pages_text = self._ocr_pdf_pages(pdf_url, pdf_bytes)
        votes: list[dict] = []
        for idx, page_text in enumerate(pages_text):
            vote = _parse_vote_page(page_text, session, canonical_names, idx, pdf_url)
            if vote:
                votes.append(vote)
        print(f"    Sparsowano {len(votes)} głosowań z {len(pages_text)} stron PDF.")
        return votes

    def build_councilors(self, all_votes, sessions, existing_profiles):
        """Override: seeduj 25 radnych z config nawet jeśli OCR coś gubi.

        Tak samo jak Rzeszów: domyślny BipScraper.build_councilors agreguje
        z all_votes, ale OCR czasem zniekształca nazwiska. Tu wstrzykujemy
        25 kanonicznych nazwisk z config żeby strona pokazywała pełną listę.
        Statystyki głosowań nakładamy na te seedy.
        """
        result = super().build_councilors(all_votes, sessions, existing_profiles)
        present = {c["name"] for c in result}
        for name in self.councilors.keys():
            if name in present:
                continue
            result.append({
                "name": name,
                "club": self.resolve_club(name),
                "district": None,
                "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
                "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
                "rebellion_count": 0, "rebellions": [],
                "has_voting_data": False, "has_activity_data": False,
            })
        result.sort(key=lambda c: c["name"])
        return result

    # -- OCR helpers ---------------------------------------------------------

    def _ocr_pdf_pages(self, pdf_url: str, pdf_bytes: bytes) -> list[str]:
        """Render PDF do PGM (pdftoppm 300dpi) i OCR (tesseract -l pol --psm 6).

        Per-page singlefile render — pdftoppm zero-paduje numer w nazwie zależnie
        od liczby stron (2 strony → 'p-1.pgm', 23 stron → 'p-01.pgm'), więc
        używamy `--singlefile` żeby uciec od tej niejednoznaczności.

        OCR text per strona cachowany na dysku.
        """
        with tempfile.TemporaryDirectory(prefix="torun_pdf_") as td:
            tdp = Path(td)
            pdf_path = tdp / "in.pdf"
            pdf_path.write_bytes(pdf_bytes)
            try:
                info = subprocess.run(
                    ["pdfinfo", str(pdf_path)],
                    capture_output=True, text=True, timeout=30,
                )
                num_pages_m = re.search(r"Pages:\s*(\d+)", info.stdout)
                num_pages = int(num_pages_m.group(1)) if num_pages_m else 0
            except Exception:
                num_pages = 0
            if num_pages == 0:
                print(f"      Brak metadanych PDF (pdfinfo), pomijam")
                return []
            pages_text: list[str] = []
            for i in range(1, num_pages + 1):
                cached = self._ocr_cache_read(pdf_url, i)
                if cached is not None:
                    pages_text.append(cached)
                    continue
                page_root = tdp / f"page-{i}"
                subprocess.run(
                    ["pdftoppm", "-r", "300", "-gray",
                     "-singlefile",
                     "-f", str(i), "-l", str(i),
                     str(pdf_path), str(page_root)],
                    timeout=120, check=False,
                )
                # singlefile output: page-N.pgm (gray) or page-N.ppm (color)
                page_pgm = page_root.with_suffix(".pgm")
                if not page_pgm.exists():
                    page_pgm = page_root.with_suffix(".ppm")
                if not page_pgm.exists():
                    print(f"      Brak renderu strony {i}")
                    pages_text.append("")
                    continue
                text = self._run_tesseract(page_pgm)
                self._ocr_cache_write(pdf_url, i, text)
                pages_text.append(text)
            return pages_text

    @staticmethod
    def _run_tesseract(image_path: Path) -> str:
        try:
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "pol", "--psm", "6"],
                capture_output=True, text=True, timeout=120,
            )
            return result.stdout
        except FileNotFoundError:
            print("      UWAGA: tesseract nie zainstalowany — OCR pomijany")
            return ""
        except subprocess.TimeoutExpired:
            print("      UWAGA: tesseract timeout")
            return ""

    def _ocr_cache_path(self, pdf_url: str, page_idx: int) -> Path | None:
        if not self.cache_dir:
            return None
        key = f"{pdf_url}#page={page_idx}".encode("utf-8")
        h = hashlib.sha1(key).hexdigest()[:16]
        return self.cache_dir / "ocr" / f"{h}.txt"

    def _ocr_cache_read(self, pdf_url: str, page_idx: int) -> str | None:
        p = self._ocr_cache_path(pdf_url, page_idx)
        if p and p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    def _ocr_cache_write(self, pdf_url: str, page_idx: int, text: str) -> None:
        p = self._ocr_cache_path(pdf_url, page_idx)
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# OCR text → vote dict
# ---------------------------------------------------------------------------

def _classify_label(token: str) -> str | None:
    for pattern, key in LABEL_PATTERNS:
        if pattern.search(token):
            return key
    return None


def _earliest_label_in_window(window: str) -> str | None:
    """Find the label that appears EARLIEST by position in `window`, not by
    LABEL_PATTERNS priority. Wiersz w PDF Torunia ma format:
        "Surname Firstname LABEL <num>. | Surname Firstname LABEL"
    czyli linia OCR-owana zawiera DWA głosowania (lewa + prawa kolumna).
    Etykieta tego radnego = pierwsza labelka po jego nazwisku.
    """
    best_pos: int | None = None
    best_key: str | None = None
    for pattern, key in LABEL_PATTERNS:
        m = pattern.search(window)
        if not m:
            continue
        if best_pos is None or m.start() < best_pos:
            best_pos = m.start()
            best_key = key
    return best_key


def _parse_vote_page(
    text: str,
    session: dict,
    canonical_names: list[str],
    vote_idx: int,
    pdf_url: str,
) -> dict | None:
    """Sparsuj jedną stronę PDF (jedno głosowanie).

    Wyciąga: DRUK NR (lub None dla poprawek), topic, data głosowania, counts,
    named_votes per radny. Mapuje nazwiska anchorem (kanoniczna lista 25)
    żeby zminimalizować szum OCR.
    """
    if not text or len(text) < 100:
        return None

    # DRUK NR (czasem 'NR.', czasem brak dla poprawek)
    druk = None
    druk_m = re.search(r"DRUK\s+N[RP][.\s]*(\d+(?:[-/]\d+)?)", text, re.IGNORECASE)
    if druk_m:
        druk = druk_m.group(1)

    # Topic: po DRUK NR ... do "Typ głosowania"
    topic = ""
    topic_m = re.search(
        r"(?:DRUK\s+N[RP][^\n]*-\s*|Poprawka[^\n]*)(.+?)(?=\n[^\n]*Typ\s+głosowania|\n[^\n]*Data\s+głosowania)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if topic_m:
        topic = re.sub(r"\s+", " ", topic_m.group(1)).strip(" .|,;:")
    if not topic:
        # fallback: linia po "Głosowanie" do "Typ głosowania"
        head_m = re.search(
            r"Głosowanie[^\n]*\n(.+?)(?=\n[^\n]*Typ\s+głosowania)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if head_m:
            topic = re.sub(r"\s+", " ", head_m.group(1)).strip(" .|,;:")
    topic = topic[:500] if topic else f"Głosowanie {vote_idx + 1}"

    # Data głosowania
    vote_date = session["date"]
    dt_m = re.search(
        r"Data\s+głosowania[.:]?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})",
        text, re.IGNORECASE,
    )
    if dt_m:
        d, mo, y = dt_m.groups()
        vote_date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # Counts (regex tolerujący OCR padding)
    counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    _SEP = r"[\s|.:,'\"]*"
    za_m = re.search(r"Głosy\s+za" + _SEP + r"(\d+)", text, re.IGNORECASE)
    przeciw_m = re.search(r"Głosy\s+przeciw" + _SEP + r"(\d+)", text, re.IGNORECASE)
    wstrz_m = re.search(r"Głosy\s+wstrzymujące\s+się" + _SEP + r"(\d+)", text, re.IGNORECASE)
    niegl_m = re.search(r"Obecni\s+niegłosujący" + _SEP + r"(\d+)", text, re.IGNORECASE)
    nieob_m = re.search(r"Liczba\s+nieobecnych" + _SEP + r"(\d+)", text, re.IGNORECASE)
    if za_m: counts["za"] = int(za_m.group(1))
    if przeciw_m: counts["przeciw"] = int(przeciw_m.group(1))
    if wstrz_m: counts["wstrzymal_sie"] = int(wstrz_m.group(1))
    if niegl_m: counts["brak_glosu"] = int(niegl_m.group(1))
    if nieob_m: counts["nieobecni"] = int(nieob_m.group(1))

    named_votes = {k: [] for k in counts}
    table_text = _extract_table_section(text)
    if table_text:
        named = _parse_named_votes(table_text, canonical_names)
        for cat, names in named.items():
            named_votes[cat].extend(names)

    total_named = sum(len(v) for v in named_votes.values())
    total_counted = sum(counts.values())
    # Jeśli OCR nie dał nic użytecznego, odrzuć (sygnal że strona była np.
    # okładką PDF, errata, albo nic do parsowania).
    if total_named == 0 and total_counted == 0:
        return None

    resolution = None
    decisive = counts["za"] + counts["przeciw"] + counts["wstrzymal_sie"]
    if decisive > 0:
        resolution = "przyjęta" if counts["za"] > decisive / 2 else "odrzucona"

    return {
        "id": f"{vote_date}_{vote_idx + 1:03d}_000",
        "source_url": pdf_url,
        "session_date": vote_date,
        "session_number": session.get("number") or session["date"],
        "topic": topic,
        "druk": druk,
        "resolution": resolution,
        "counts": counts,
        "named_votes": named_votes,
    }


def _extract_table_section(text: str) -> str:
    """Wytnij sekcję z 25 wierszami radnych ('Uprawnieni do głosowania' →
    'Wydrukowano:'). Reszta strony PDF to nagłówki/counts/footer."""
    start_m = re.search(r"Uprawnieni\s+do\s+głosowania", text, re.IGNORECASE)
    body = text[start_m.end():] if start_m else text
    end_m = re.search(r"Wydrukowano[.:]", body, re.IGNORECASE)
    if end_m:
        body = body[:end_m.start()]
    return body


def _parse_named_votes(table_text: str, canonical_names: list[str]) -> dict[str, list[str]]:
    """Dla każdego z 25 kanonicznych nazwisk znajdź wiersz w OCR text i
    wyciągnij etykietę głosu. Anchor po surname (znormalizowane: bez
    diakrytyków, lowercase).

    Strategia dwustopniowa:
      1. Exact substring match po znormalizowanym surname.
      2. Fallback fuzzy: dla pozostałych szukamy najbliższego ~8-12-znakowego
         substringu w tekście używając difflib (tolerancja na OCR przekręty
         pojedynczych liter, np. 'Koziołocki' → 'Koziałocki').
    """
    from difflib import SequenceMatcher

    out: dict[str, list[str]] = {
        "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": [],
    }
    norm_text = _normalize_for_match(table_text)

    matched: dict[str, int] = {}  # canonical → match position in table_text
    unmatched: list[str] = []
    for canonical in canonical_names:
        parts = canonical.split()
        if not parts:
            continue
        surname = parts[-1]
        norm_surname = _normalize_for_match(surname)
        idx = norm_text.find(norm_surname)
        if idx < 0:
            unmatched.append(canonical)
            continue
        matched[canonical] = idx

    # Fuzzy fallback dla nieznalezionych. Sliding window po długości surname.
    if unmatched:
        # Wszystkie pozycje są "zajęte" przez exact matches plus marginal
        # bufor, żeby fuzzy nie nakładał się na exact match.
        for canonical in unmatched:
            parts = canonical.split()
            surname = parts[-1]
            norm_surname = _normalize_for_match(surname)
            L = len(norm_surname)
            if L < 4:
                continue
            best_ratio = 0.0
            best_pos = -1
            # Skip already-used positions ± 0.5*L.
            used_ranges = sorted(matched.values())
            for i in range(len(norm_text) - L + 1):
                # Skip jeśli ta pozycja jest blisko już zmatchowanego surname.
                if any(abs(i - u) < L // 2 for u in used_ranges):
                    continue
                substr = norm_text[i : i + L]
                # Tania heurystyka: pierwsza i ostatnia litera muszą się zgadzać.
                if substr[0] != norm_surname[0] or substr[-1] != norm_surname[-1]:
                    continue
                r = SequenceMatcher(None, substr, norm_surname).ratio()
                if r > best_ratio:
                    best_ratio = r
                    best_pos = i
            if best_ratio >= 0.75 and best_pos >= 0:
                matched[canonical] = best_pos

    for canonical, idx in matched.items():
        line_end = norm_text.find("\n", idx)
        if line_end < 0:
            line_end = min(idx + 200, len(norm_text))
        window = table_text[idx:line_end]
        label_key = _earliest_label_in_window(window)
        if not label_key:
            continue
        out[label_key].append(canonical)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_councilors(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Radoskop Toruń")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--incremental-window", type=int, default=30)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    config_path = HERE.parent.parent / "config.json"
    councilors = load_councilors(config_path)

    scraper = TorunScraper(
        base_url=BASE_URL,
        kadencje=KADENCJE,
        councilors=councilors,
        delay=args.delay,
        cache_dir=args.cache_dir,
        default_kadencja="2024-2029",
    )
    return scraper.run(
        output_path=args.output,
        profiles_path=args.profiles,
        max_sessions=args.max_sessions or 0,
        dry_run=args.dry_run,
        incremental_window_days=args.incremental_window,
        force_full=args.full,
    )


if __name__ == "__main__":
    raise SystemExit(main())
