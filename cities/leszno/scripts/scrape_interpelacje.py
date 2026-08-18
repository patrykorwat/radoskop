#!/usr/bin/env python3
"""Scraper interpelacji radnych Rady Miejskiej w Lesznie.

Źródło: **eSesja jest NIEAKTYWNA** (leszno.esesja.pl/interpelacje_i_zapytania →
"Brak aktywności lub moduł nieaktywny"). Prawdziwy rejestr znajduje się w BIP
Leszna (CMS Logonet, jak Kowary):

    Kategoria rejestru:   https://bip.leszno.pl/artykuly/171/interpelacje-radnych-rady-miejskiej
    Pełny rejestr:        https://bip.leszno.pl/artykul/171/13453/interpelacje-wg-kolejnosci-chronologicznej

Rejestr = JEDEN artykuł z załącznikami PDF (jedna interpelacja = jeden załącznik
o nazwie "N_Interpelacja_Radny_DD_MM_YYYY", a obok odpowiedź
"..._ODPOWIEDZ"). Nie ma stron szczegółów ani tabel z przedmiotem — przedmiot
znajduje się w środku zeskanowanych (w większości ręcznie wypełnianych) formularzy.

Co jest wiarygodne i maszynowo odczytywalne (bierzemy z nazw plików + metryczek BIP):
    cri (numer), radny, data_wplywu, odpowiedz_status (odpowiedź wg obecności
    pliku _ODPOWIEDZ), odpowiedz_url, data_odpowiedzi (Data wytworzenia pliku
    _ODPOWIEDZ), tresc_url, bip_url, klub (z config.json club_assignments->clubs).

Ograniczenie przedmiotu: treść to zeskanowany, ręcznie pisany
formularz — nie ma tekstowej warstwy. Przedmiot staramy się odczytać OCR-em
(tesseract, język polski) z pierwszego skanu; jakość może być ograniczona.
Gdy OCR nic nie da, przedmiot zostaje pusty (nie fabrykujemy).

Output: rekordy w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /cache/interp/leszno
    python3 scrape_interpelacje.py --output docs/interpelacje.json --ocr  # najlepszy wysiłek przedmiotu
"""

import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache, cached_fetch_text  # noqa: E402

from difflib import SequenceMatcher

BASE = "https://bip.leszno.pl"
REGISTER_URL = f"{BASE}/artykul/171/13453/interpelacje-wg-kolejnosci-chronologicznej"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.4
MIN_ROK_DEFAULT = 2024

_DEBUG = False
_CLUB_ASSIGN = {}
_CLUBS = {}


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> None:
    global _CLUB_ASSIGN, _CLUBS
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return
    _CLUB_ASSIGN = cfg.get("club_assignments", {}) or {}
    _CLUBS = cfg.get("clubs", {}) or {}


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _abs(url: str) -> str:
    return url if url.startswith("http") else BASE + url


# ---------------------------------------------------------------------------
# Parsing rejestru (artykuł z załącznikami)
# ---------------------------------------------------------------------------

# Każdy załącznik ma blok:
#   <div class="header"> ... <a id="attachments-title" href=".../download/NNN"> TYTUL </a> ...
#   <div class="legal file_legal_N"> <table> <tr><th>X</th><td>Y</td></tr> ... </table>
_TITLE_RE = re.compile(
    r'<a\s+id="attachments-title"[^>]*href="(https://bip\.leszno\.pl/attachments/download/(\d+))"[^>]*>\s*(.*?)\s*</a>',
    re.S,
)
_TH_TD_RE = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _legal_meta(segment: str) -> dict:
    meta = {}
    for k, v in _TH_TD_RE.findall(segment):
        meta[_clean(k).rstrip(":")] = _clean(v)
    dt = re.findall(r'<time datetime="([^"]+)"', segment)
    meta["_datetimes"] = dt
    return meta


def parse_attachments(html: str) -> list[dict]:
    """Zwraca listę załączników: {id, url, title, cri, radny_ascii, meta}."""
    if not html:
        return []
    titles = list(_TITLE_RE.finditer(html))
    out = []
    for i, m in enumerate(titles):
        url, attid, title_html = m.group(1), m.group(2), m.group(3)
        title = _clean(title_html)
        seg = html[m.end(): titles[i + 1].start()] if i + 1 < len(titles) else html[m.end(): m.end() + 8000]
        meta = _legal_meta(seg)
        cri = ""
        if title:
            mm = re.match(r"^\s*(\d+)", title)
            if mm:
                cri = mm.group(1)
        out.append({
            "id": attid,
            "url": url,
            "title": title,
            "cri": cri,
            "meta": meta,
        })
    return out


def _clean_radny_candidate(name: str) -> str:
    """Czyści wartość 'Odpowiedzialny za treść': usuwa prefiks 'Interpelacja' itp."""
    n = re.sub(r"^\s*(Interpelacja|Zapytanie|Wniosek|Petycja)\b\s*", "", (name or "").strip())
    return n.strip()


def _radny_from_filename(title: str) -> str:
    """'N_Interpelacja_Radny_DD_MM_YYYY' -> 'Radny' (ASCII, może mieć myślniki)."""
    parts = [p for p in title.replace(".", "_").split("_") if p]
    # numer, [ew. 'Interpelacja'], radny..., DD, MM, RRRR
    idx = 1
    if parts and parts[0].isdigit():
        idx = 1
    if len(parts) > idx and parts[idx].lower().startswith("interpelacja"):
        idx += 1
    name_tokens = parts[idx:-3] if len(parts) >= idx + 3 else parts[idx:]
    return " ".join(name_tokens).strip() if name_tokens else ""


def _resolve_radny(metrics: dict, title: str) -> str:
    """Wybiera najlepsze dopasowanie nazwiska radnego do club_assignments.

    Metryczka BIP bywa z literówkami / ASCII (np. 'Andzelika Szkudlarek-Kuźniak',
    'Łukasz Woźnaik') albo z prefiksem 'Interpelacja'. Krzyżujemy kandydatów z
    metryczki i nazwy pliku, fuzzy-dopasowujemy do kluczy config (mianownik).
    """
    candidates = []
    meta_name = _clean_radny_candidate(
        metrics.get("Odpowiedzialny za treść") or metrics.get("Wytworzył") or ""
    )
    if meta_name:
        candidates.append(meta_name)
    fn_name = _radny_from_filename(title)
    if fn_name:
        candidates.append(fn_name)

    if not candidates:
        return ""

    best_key, best_ratio = "", 0.0
    for cand in candidates:
        c = cand.lower()
        # dokładne trafienie -> od razu
        if cand in _CLUB_ASSIGN:
            return cand
        for key in _CLUB_ASSIGN:
            ratio = SequenceMatcher(None, c, key.lower()).ratio()
            if ratio > best_ratio:
                best_ratio, best_key = ratio, key
    if best_ratio >= 0.72:
        return best_key
    # brak sensownego trafienia: zwracamy najładniejszego kandydata
    return candidates[0]


def build_records(attachments: list[dict]) -> list[dict]:
    """Grupuje załączniki po numerze interpelacji; zwraca rekordy Radoskop."""
    # Indeksujemy: cri -> dict z polami tresc / odpowiedz / radny / data
    grouped: dict[str, dict] = {}
    for att in attachments:
        cri = att["cri"]
        if not cri:
            continue
        title = att["title"]
        low = title.lower()
        is_answer = "_odpowiedz" in low
        is_primary = "_interpelacja" in low and not is_answer

        g = grouped.setdefault(cri, {
            "tresc": None, "odpowiedz": None, "radny": "", "data": "",
            "radny_ascii": "", "tytuly": [], "zase_ok": [],
        })
        g["tytuly"].append(title)

        if is_answer:
            if g["odpowiedz"] is None:
                g["odpowiedz"] = att
        elif is_primary:
            meta = att["meta"]
            radny = _resolve_radny(meta, title)
            # Data wytworzenia z metryczki (authoritative) -> RRRR-MM-DD
            dt = ""
            for d in meta.get("_datetimes", []):
                if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    dt = d[:10]
                    break
            if g["tresc"] is None:
                g["tresc"] = att
                g["radny"] = radny
                g["data"] = dt
        # (załączniki dodatkowe ignorujemy do rekordu)

    records = []
    for cri, g in grouped.items():
        if g["tresc"] is None:
            continue
        radny = g["radny"]
        data = g["data"] or ""
        rok = 0
        if data:
            try:
                rok = int(data[:4])
            except ValueError:
                rok = 0
        odpowiedz = g["odpowiedz"]
        data_odpowiedzi = ""
        if odpowiedz:
            for d in odpowiedz["meta"].get("_datetimes", []):
                if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    data_odpowiedzi = d[:10]
                    break
        records.append({
            "cri": cri,
            "typ": "interpelacja",
            "rok": rok,
            "kadencja": "2024-2029" if rok >= 2024 else "",
            "radny": radny,
            "przedmiot": "",
            "data_wplywu": data,
            "klub": _club_for_radny(radny),
            "odpowiedz_status": "Udzielono" if odpowiedz else "Nie udzielono",
            "tresc_url": g["tresc"]["url"] if g["tresc"] else "",
            "odpowiedz_url": odpowiedz["url"] if odpowiedz else "",
            "data_odpowiedzi": data_odpowiedzi,
            "bip_url": g["tresc"]["url"] if g["tresc"] else "",
        })
    records.sort(key=lambda r: (r["data_wplywu"] or "", int(r["cri"] or 0)))
    return records


# ---------------------------------------------------------------------------
# Przedmiot — best-effort OCR zeskanowanych formularzy (wymagany tesseract)
# ---------------------------------------------------------------------------

def _ocr_first_page(pdf_bytes: bytes) -> str:
    """Wyciąga tekst za pomocą OCR z pierwszego skanu PDF (pol)."""
    try:
        from pypdf import PdfReader
        from PIL import Image
    except Exception:
        return ""
    try:
        rdr = PdfReader(io.BytesIO(pdf_bytes))
        page = rdr.pages[0]
        images = list(page.images) if hasattr(page, "images") else []
        if not images:
            return ""
        # Wybieramy największy obraz (właściwy skan strony), nie symbole.
        best = None
        for im in images:
            try:
                size = len(im.data)
                if best is None or size > len(best.data):
                    best = im
            except Exception:
                pass
        if best is None:
            return ""
        pil = Image.open(io.BytesIO(best.data)).convert("L")
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        out = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "pol", "--psm", "6"],
            input=buf.getvalue(), capture_output=True, timeout=60,
        )
        if out.returncode != 0:
            return ""
        text = (out.stdout or b"").decode("utf-8", "ignore")
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return ""


def ocr_przedmiot(ocr_text: str) -> str:
    """Z tekstu OCR wybiera linię przedmiotu (Treść interpelacji...)."""
    if not ocr_text:
        return ""
    m = re.search(r"Treść interpelacji / zapytania[^:]*:\s*(.+)", ocr_text)
    if m:
        return m.group(1).strip()[:300]
    # Fallback: pierwsza rozsądnej długości linia (dicta) — unikamy nagłówków formularza.
    for line in re.split(r"[;.]\s*", ocr_text):
        line = line.strip()
        if 20 <= len(line) <= 300 and not re.match(r"^(Leszno|Prezydent|Rada|Nr|Imię)", line, re.I):
            return line[:300]
    return ""


def fetch_binary(session: requests.Session, url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=40)
            if resp.status_code == 200 and resp.content:
                return resp.content
            _log(f"  {resp.status_code} {url}")
            if resp.status_code in (403, 429):
                import time
                time.sleep(3)
        except requests.RequestException:
            import time
            time.sleep(2)
    return b""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji radnych z BIP Leszna (rejestr załączników PDF)"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Opcjonalnie: odczytaj przedmiot OCR-em z zeskanowanych formularzy "
        "(domyślnie PRZEDMIOT jest PUSTY — treść w BIP to ręcznie pisane skany, "
        "OCR daje niską jakość; nie fabrykujemy)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrapuj też wcześniejsze kadencje; domyślnie tylko 2024-2029",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)
    _load_clubs()
    session = _session()

    print("=== Interpelacje Radnych — BIP Leszno ===")
    html = cached_fetch_text(REGISTER_URL, session=session, headers=HEADERS, delay=DELAY)
    if not html:
        print("  [błąd] brak treści rejestru:", REGISTER_URL)
        return 2
    attachments = parse_attachments(html)
    print(f"  Załączników w rejestrze: {len(attachments)}")

    records = build_records(attachments)
    if min_rok:
        records = [r for r in records if not r["rok"] or r["rok"] >= min_rok]
    print(f"  Rekordów interpelacji (2024-2029): {len(records)}")

    # Przedmiot — tylko przy jawnym --ocr (domyślnie pusty; nie fabrykujemy)
    if args.ocr and records:
        print("  Odczytywanie przedmiotu OCR-em (zeskanowane formularze)...")
        import time
        done = 0
        for i, rec in enumerate(records):
            pdf = fetch_binary(session, rec["tresc_url"])
            if not pdf:
                continue
            txt = _ocr_first_page(pdf)
            rec["przedmiot"] = ocr_przedmiot(txt)
            done += 1
            if done % 25 == 0 or i == len(records) - 1:
                print(f"    {i + 1}/{len(records)} (przedmiot u {done})")
            time.sleep(DELAY)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    with_subj = sum(1 for r in records if r["przedmiot"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:        {interp}")
    print(f"Z odpowiedzią:       {answered}")
    print(f"Z przedmiotem (OCR): {with_subj}")
    print(f"Razem:               {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
