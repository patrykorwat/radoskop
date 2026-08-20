#!/usr/bin/env python3
"""Scraper interpelacji/zapytań radnych Rady Miejskiej w Świętochłowicach.

Źródło: **eSesja NIEAKTYWNA** (swietochlowice.esesja.pl → "Brak aktywności lub
moduł nieaktywny"). Prawdziwy rejestr jest w BIP Świętochłowic (CMS Liferay bipkod):

    Rada Miejska → Wystąpienia, interpelacje, wnioski, zapytania, oświadczenia
    → lata 2024/2025/2026:
        https://www.bip.swietochlowice.pl/bipkod/34003387  (2024)
        https://www.bip.swietochlowice.pl/bipkod/37924950  (2025)
        https://www.bip.swietochlowice.pl/bipkod/42308250  (2026)

Struktura: każdy rok to zbiór "sesji"; w każdej sesji pliki PDF:
  - 1 plik ZBIORCZY ("Interpelacje i zapytania radnych sformułowane na N Sesji
    Rady Miejskiej …" / "…złożone w trakcie obrad N Sesji…") – zawiera wszystkie
    wystąpienia sesji (NIEROZDZIELNE per radny),
  - pliki POSZCZEGÓLNE (międzysesyjne): "Interpelacja międzysesyjna {radnego/-nej} X
    z dnia DD.MM.RRRR" / "Zapytanie {radnego/nej} X z dnia …" – jeden na radnego,
  - pliki ODPOWIEDZI: "Odpowiedź na interpelację/zapytanie - {Radny}".

Ten scraper buduje rekordy WYŁĄCZNIE z plików poszczególnych (pojedynczy autor +
data w nazwie) — to część rejestru, którą da się wiarygodnie przypisać do radnego.
Pliki zbiorcze (sesyjne) pomijamy (nie da się ich rozbić per radny z nazwy), co
jest uczciwą, jawną granicą źródła (partial). Odpowiedź dopasowujemy wg radnego
w obrębie tej samej sesji.

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json [--cache-dir DIR]
"""

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache, cached_fetch_text  # noqa: E402

BASE = "https://www.bip.swietochlowice.pl"
YEAR_PAGES = [
    f"{BASE}/bipkod/34003387",  # 2024
    f"{BASE}/bipkod/37924950",  # 2025
    f"{BASE}/bipkod/42308250",  # 2026
]
YEAR_OF = {34003387: 2024, 37924950: 2025, 42308250: 2026}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.4
MIN_ROK_DEFAULT = 2024

_CLUB_ASSIGN = {}
_CLUBS = {}

_DOC_LABEL_RE = re.compile(r'attachments-label">\s*Dokumenty:', re.I)
_DOC_FILE_RE = re.compile(r'<div\s+class="article-document-file"\s+data-document-id="(\d+)"', re.S)
_FILE_TITLE_RE = re.compile(r'<a[^>]+href="([^"]*res/serwisy/pliki/(\d+)[^"]*)"[^>]*title="([^"]+)"', re.S)
_FILE_DATE_RE = re.compile(r'<div\s+class="span3\s+date">\s*([^<]+?)\s*</div>', re.S)


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


def _clean(s: str) -> str:
    import html as _h
    s = re.sub(r"&#8209;", "-", s or "")
    s = re.sub(r"&nbsp;", " ", s)
    s = _h.unescape(s)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[ęóąśłżźćń]", lambda m: {
        "ę": "e", "ó": "o", "ą": "a", "ś": "s", "ł": "l",
        "ż": "z", "ź": "z", "ć": "c", "ń": "n",
    }[m.group(0)], s)
    return re.sub(r"[^a-z0-9]+", "", s)


_MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}


def _parse_date(s: str) -> str:
    if not s:
        return ""
    low = (s or "").lower()
    m = re.search(r"(\d{1,2})\s+([a-ząęółśżźćń]+)\s+(\d{4})", low)
    if m and m.group(2) in _MONTHS_PL:
        return f"{int(m.group(3)):04d}-{_MONTHS_PL[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})[.\-\s/](1[0-2]|0[1-9])[.\-\s/](20\d\d)", low)
    if m:
        try:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        except Exception:
            return ""
    return ""


def _parse_bip_date(s: str) -> str:
    m = re.search(r"(\d{1,2})[‑\-\./](\d{1,2})[‑\-\./](20\d\d)", s or "")
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def _match_surname(surname: str) -> str:
    """Genitive→nominative→config club_assignments (imię+nazwisko)."""
    if not surname:
        return ""
    cand = _norm(surname)
    variants = {cand}
    for suf, rep in [("iego", "y"), ("ego", "y"), ("iej", "a"), ("ej", "a"),
                     ("owicza", ""), ("y", "a"), ("a", "")]:
        if len(cand) > len(suf) and cand.endswith(suf):
            variants.add(cand[: -len(suf)] + rep)
            variants.add(cand[: -len(suf)])
    for key in _CLUB_ASSIGN:
        last = _norm(key.split()[-1])
        for v in list(variants):
            if v and (v == last or last.endswith(v)
                      or SequenceMatcher(None, v, last).ratio() >= 0.85):
                return key
    best, brat = "", 0.0
    for key in _CLUB_ASSIGN:
        last = _norm(key.split()[-1])
        r = SequenceMatcher(None, cand, last).ratio()
        if r > brat:
            best, brat = key, r
    return best if brat >= 0.7 else ""


def _is_aggregate(title: str) -> bool:
    low = title.lower()
    if re.search(r"zbiór|zbiorczy", low):
        return True
    # wiele osób / sesja zbiorowa
    if ("sformu." in low and "na " in low and "sesji" in low):
        return True
    if re.search(r"złożone w trakcie obrad .* sesji", low):
        return True
    if ("i zapytania" in low or "i zapytanie" in low) and ("sesji" in low or "międzysesyjne" in low):
        # 'Interpelacje i zapytania radnych złożone w okresie międzysesyjnym w dniu...' zbiorcze
        if "radnych" in low and "w dniu" in low:
            return True
    # dwie osoby: '... X i radnej Y ...' / '... X oraz radnej Y ...'
    if re.search(r"\b(?:i|oraz)\s+(?:radnej|radnego|przewodnicz|wiceprzewodnicz)", low):
        return True
    if "radnego" in low and "radnej" in low:
        return True
    return False


def _is_answer(title: str) -> bool:
    low = title.lower()
    return low.startswith("odpowied") or "odpowiedź na" in low or "odpowiedźna" in low


def _radny_from_filename(title: str) -> str:
    """Wyciąga nazwisko (dopełniacz) po markerze autora."""
    m = re.search(
        r"(?:radnej|radnego)\s+(?:pana\s+|pani\s+)?([A-ZĄĆĘŁŃÓŚŹŻ][^0-9]{2,60}?)(?=\s+z\s+(?:dnia|łoż)|\s+w\s+spraw|\s*z\s+dn|\s*\.pdf|$)",
        title, re.I)
    if not m:
        m = re.search(
            r"(?:Wiceprzewodnicz|Przewodnicz)[a-ząęółśżźćń]*\s+Rady\s+Miejskiej\s+([A-ZĄĆĘŁŃÓŚŹŻ][^0-9]{2,60}?)(?=\s+z\s+(?:dnia|łoż)|\s+w\s+spraw|\s*z\s+dn|\s*\.pdf|$)",
            title, re.I)
    if not m:
        return ""
    name = re.sub(r"[,\s]+$", " ", m.group(1)).strip()
    name = re.sub(r"\s+", " ", name)
    # dwie osoby w tym samym segmencie -> pomiń (nie przypisuj błędnie)
    if re.search(r"\b(?:i|oraz)\b", name):
        return ""
    toks = name.split()
    surname = toks[-1] if toks else ""
    surname = re.sub(r"[\.:;]", "", surname)
    return _match_surname(surname)


def _subject_from_filename(title: str) -> str:
    low = title.lower()
    m = re.search(r"\bw\s+spr[a-z]*\.?\s*[:]?\s*(.+)", low)
    if m:
        subj = re.sub(r"\s+", " ", m.group(1)).strip(" .:;-")
        return subj
    return ""


def parse_files_in_session(html: str, seg_start: int, seg_end: int):
    """Zwraca listę plików w obrębie jednej sesji: {id,url,title,date_added}."""
    out = []
    for m in _DOC_FILE_RE.finditer(html, seg_start, seg_end):
        sub = html[m.start():min(m.end() + 900, seg_end)]
        tm = _FILE_TITLE_RE.search(sub)
        if not tm:
            continue
        dm = _FILE_DATE_RE.search(sub)
        out.append({
            "id": m.group(1), "url": tm.group(1) if tm.group(1).startswith("http") else BASE + "/" + tm.group(1).lstrip("/"),
            "title": _clean(tm.group(3).split("/ Identyfikator")[0]),
            "date_added": _parse_bip_date(_clean(dm.group(1))) if dm else "",
        })
    return out


def process_years(session_get) -> list[dict]:
    records = []
    # grupy wg imienia klucza roku
    for pid, year in YEAR_OF.items():
        html = session_get(f"{BASE}/bipkod/{pid}")
        labels = [m.start() for m in _DOC_LABEL_RE.finditer(html)]
        bnd = labels + [len(html)]
        for si, lab in enumerate(labels):
            seg_start = lab
            seg_end = bnd[si + 1]
            files = parse_files_in_session(html, seg_start, seg_end)
            # pliki treści (pomijamy odpowiedzi; agregaty/wieloosobowe odpadają
            # w _radny_from_filename przez brak pojedynczego radnego)
            tresc_files = [f for f in files if not _is_answer(f["title"])]
            ans_files = [f for f in files if _is_answer(f["title"])]
            for tf in tresc_files:
                typ = "zapytanie" if re.search(r"\bzapytanie\b", tf["title"], re.I) and not re.search(r"\binterpelacja\b", tf["title"], re.I) else "interpelacja"
                radny = _radny_from_filename(tf["title"])
                if not radny:
                    # plik zbiorczy/nieprzypisywalny do radnego -> pomijamy (partial, honest)
                    continue
                data = _parse_date(tf["title"]) or tf["date_added"]
                rok = int(data[:4]) if data else year
                if rok and rok < MIN_ROK_DEFAULT:
                    continue
                kad = "2024-2029" if rok >= 2024 else ("2018-2024" if 2018 <= rok < 2024 else "")
                # odpowiedź po radnym w tej sesji
                odp = None
                if radny:
                    rlast = _norm(radny.split()[-1])
                    for af in ans_files:
                        amid = _match_surname_from_ans(af["title"])
                        if amid and amid == radny:
                            odp = af
                            break
                    if odp is None:
                        for af in ans_files:
                            afn = _norm(af["title"].split("-")[-1].strip())
                            if rlast and (rlast in afn or afn.endswith(rlast) or SequenceMatcher(None, rlast, afn).ratio() >= 0.7):
                                odp = af
                                break
                records.append({
                    "cri": tf["id"],
                    "typ": typ,
                    "rok": rok,
                    "kadencja": kad,
                    "radny": radny,
                    "przedmiot": _subject_from_filename(tf["title"]),
                    "data_wplywu": data,
                    "klub": _club_for_radny(radny),
                    "odpowiedz_status": "Udzielono" if odp else "Nie udzielono",
                    "tresc_url": tf["url"],
                    "odpowiedz_url": odp["url"] if odp else "",
                    "data_odpowiedzi": odp["date_added"] if odp else "",
                    "bip_url": tf["url"],
                })
    seen = set()
    uniq = []
    for r in sorted(records, key=lambda x: (x["data_wplywu"] or "9999", x["cri"])):
        if r["tresc_url"] in seen:
            continue
        seen.add(r["tresc_url"])
        uniq.append(r)
    return uniq


def _match_surname_from_ans(title: str) -> str:
    """Z tytułu odpowiedzi 'Odpowiedź na interpelację - Radny X' zwraca radny."""
    part = title.split("-", 1)[-1]
    part = re.sub(r"\(\d+[^)]*\)", " ", part)
    toks = [t for t in part.replace(",", " ").split() if t]
    fun = re.search(r"(?:Wiceprzewodnicz|Przewodnicz)[a-ząęółśżźćń]*|Rady Miejskiej", part, re.I)
    if fun and "Rady Miejskiej" in part:
        m = re.search(r"(?:Wiceprzewodnicz|Przewodnicz)[a-ząęółśżźćń]*\s+Rady\s+Miejskiej\s+([A-ZĄĆĘŁŃÓŚŹŻ][^,;0-9]{2,30}?)\b", part, re.I)
        if m:
            return _match_surname(re.sub(r"[\.:;]", "", m.group(1).split()[-1]))
    # bierzemy nagłówki 'nie-answers' i szukamy ostatniego przez 'radny X'
    m = re.search(r"(?:radnego|radnej|radny|radna)\s+([A-ZĄĆĘŁŃÓŚŹŻ][^,;0-9]{2,40}?)\b", part, re.I)
    if m:
        cand = m.group(1).split()[-1]
        return _match_surname(re.sub(r"[\.:;]", "", cand))
    # fallback: nazwisko = ostatni wyraz
    last = toks[-1] if toks else ""
    return _match_surname(re.sub(r"[\.:;]", "", last))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ocr", action="store_true")
    args = ap.parse_args()

    if args.cache_dir:
        init_cache(args.cache_dir)
    _load_clubs()

    def session_get(url, _sm=None):
        return cached_fetch_text(url) if args.cache_dir else requests.get(url, headers=HEADERS, timeout=40).text

    records = process_years(session_get)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[swietochlowice] rekordy={len(records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
