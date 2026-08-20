#!/usr/bin/env python3
"""Scraper interpelacji/zapytań radnych Rady Miejskiej w Szczyrku.

Źródło: **eSesja NIEAKTYWNA** (szczyrk.esesja.pl/interpelacje_i_zapytania →
"Brak aktywności lub moduł nieaktywny"). Prawdziwy rejestr jest w BIP Szczyrka
(CMS Liferay bipkod):

    Rada Miejska 2024-2029 → ""Interpelacje i zapytania radnych oraz udzielone odpowiedzi""
    https://www.bip.szczyrk.pl/bipkod/35161585

Struktura (artykuł BIP = rejestr): N grup; każda grupa to
    [opis / przedmiot w treści artykułu]
    <div class="attachments-label">Dokumenty:</div>
    <div class="article-document-file" data-document-id="..">  PLIK-interpelacji
    <div class="article-document-file" ...>                    PLIK-odpowiedzi
Dla każdego pliku kolumna `<div class="span3 date">DD-MM-YYYY` (data dodania do BIP).

Co jest wiarygodne i maszynowo odczytywalne:
    cri (data-document-id), typ (interpelacja/zapytanie z nazwy pliku),
    radny (nazwisko z nazwy pliku, fuzzy→config, inaczej puste),
    przedmiot (z opisu/treści artykułu; gdy brak — z nazwy pliku "w spr./dot.",
               inaczej puste — nie fabrykujemy),
    data (z nazwy pliku/opisu; fallback=data dodania pliku),
    odpowiedz_status + odpowiedz_url (plik-odpowiedź w tej samej grupie),
    klub (z config.json club_assignments->clubs).

Wszystkie rekordy są realne (pochodzą z jawnych danych BIP); gdzie dane są
nieodczytywalne pozostawiamy puste pole zamiast zgadywać.

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

BASE = "https://www.bip.szczyrk.pl"
REGISTER_URL = f"{BASE}/bipkod/35161585"

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

# znaczniki struktury Liferay bipkod
_DOC_LABEL_RE = re.compile(r'attachments-label">\s*Dokumenty:', re.I)
_DOC_FILE_RE = re.compile(
    r'<div\s+class="article-document-file"\s+data-document-id="(\d+)"(.*?)</div>',
    re.S,
)
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


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False  # BIP Szczyrka ma cert-hostname-mismatch; treść jawna
    return s


def _abs(url: str) -> str:
    return url if url.startswith("http") else BASE + "/" + url.lstrip("/")


def _clean(s: str) -> str:
    s = re.sub(r"&#8209;", "-", s or "")
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&#324;", "ń", s)
    s = re.sub(r"&#380;", "ż", s)
    s = re.sub(r"&#322;", "ł", s)
    s = re.sub(r"&[a-z]+;", " ", s)
    s = H_unescape(s)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def H_unescape(s: str) -> str:
    try:
        import html as _h
        return _h.unescape(s)
    except Exception:
        return s


_MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}


def _date_clean(s: str) -> str:
    """Usuwa szum (referencje ORSA i frazy 'w spr.'/'dot.') przed parsowaniem daty,
    żeby numer referencji nie był brany za datę."""
    low = (s or "").lower()
    low = re.sub(r"orsa[\w.\- ]{0,30}?\d{2,4}", " ", low)   # ORSA.0003.12.2025 ...
    low = re.sub(r"\bw\s+spr[a-z]*\.?\b", " ", low)
    low = re.sub(r"\bdot\.?\b", " ", low)
    low = re.sub(r"\b[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{2,4}\b", " REF ", low)
    return low


def _parse_date(s: str) -> str:
    """Różne formaty daty -> RRRR-MM-DD ('' gdy brak)."""
    if not s:
        return ""
    low = _date_clean(s).strip()
    m = re.search(r"(\d{1,2})\s+([a-ząęółśżźćń]+)\s+(\d{4})", low)
    if m and m.group(2) in _MONTHS_PL:
        return f"{int(m.group(3)):04d}-{_MONTHS_PL[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})[.\-\s/](1[0-2]|0?[1-9])[.\-\s/](20\d\d)", low)
    if m:
        d, mo, y = m.group(1), int(m.group(2)), m.group(3)
        try:
            return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
        except Exception:
            return ""
    return ""


def _parse_bip_date(s: str) -> str:
    """'17‑03‑2026 08:21:59' (data dodania) -> RRRR-MM-DD."""
    m = re.search(r"(\d{1,2})[‑\-\./](\d{1,2})[‑\-\./](20\d\d)", s or "")
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[ęóąśłżźćń]", lambda m: {
        "ę": "e", "ó": "o", "ą": "a", "ś": "s", "ł": "l",
        "ż": "z", "ź": "z", "ć": "c", "ń": "n",
    }[m.group(0)], s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _radny_from_filename(title: str) -> str:
    low = title.lower().replace(".pdf", "")
    low = re.sub(r"^\s*(odp\.?|odpowiedz[ź]?|interpelacja|interp|zapytanie|zapyt|wniosek)\b[\s.\-]*", "", low)
    low = re.sub(r"\b(na|w|spr\.?|dot\.?|z|dnia|r\.?|bip|i|oraz)\b", " ", low)
    low = re.sub(r"orsa[.\-\s0-9]*", " ", low)
    low = re.sub(r"\d{1,2}[.\-\s/]\d{1,2}[.\-\s/]\d{2,4}", " ", low)
    low = re.sub(r"\b\d{4}\b", " ", low)
    toks = [t for t in low.split() if t]
    if not toks:
        return ""
    # pierwsze słowo zaczynające się wielką literą = nazwisko
    for t in toks:
        if t[0].isupper():
            return t
    return toks[0].capitalize()


def _resolve_radny(cands) -> str:
    for cand in cands:
        if not cand:
            continue
        if cand in _CLUB_ASSIGN:
            return cand
        cn = _norm(cand)
        best, ratio = "", 0.0
        for key in _CLUB_ASSIGN:
            last = _norm(key.split()[-1])
            r = SequenceMatcher(None, cn, last).ratio()
            if r > ratio:
                best, ratio = key, r
        if ratio >= 0.8:
            return best
    # brak wiarygodnego dopasowania do radnego (np. autor zbiorowy / brak nazwiska
    # w nazwie pliku) -> puste, NIE zgadujemy
    return ""


def _subj_from_filename(title: str) -> str:
    low = title.lower()
    m = re.search(r"\bw\s+spr[a-z]*\.?\s+(.+?)(\.pdf|$)", low)
    if m:
        return m.group(1).strip(" .-")
    m = re.search(r"\b(?:dot\.?|dotyczy)\s+(.+?)(\.pdf|$)", low)
    if m:
        return m.group(1).strip(" .-")
    return ""


def _is_answer(t: str) -> bool:
    low = (t or "").lower()
    return bool(low.startswith("odp") or "odpowied" in low or " odpowied" in low
                or re.search(r"\bodp\.?\b", low) or low.startswith("odpowied"))


def parse_groups(html: str):
    """Zwraca listę grup rejestru: {desc, files:[{id,url,title,date_added}]}.

    Struktura Liferay bipkod:  [opis(tytuł wystąpienia)] ["Dokumenty:"] [plik-treść] [plik-odpowiedź]
    Opis (tytuł wystąpienia) znajduje się w oknie tuż przed etykietą "Dokumenty:".
    """
    labels = [m.start() for m in _DOC_LABEL_RE.finditer(html or "")]
    blocks = list(_DOC_FILE_RE.finditer(html or ""))
    docs = []
    for bi, m in enumerate(blocks):
        seg_end = blocks[bi + 1].start() if bi + 1 < len(blocks) else m.start() + 4000
        seg = html[m.start():seg_end]
        tm = _FILE_TITLE_RE.search(seg)
        if not tm:
            continue
        dm = _FILE_DATE_RE.search(seg)
        date_raw = _clean(dm.group(1)) if dm else ""
        docs.append({
            "id": m.group(1), "url": _abs(tm.group(1)),
            "title": tm.group(3).split("/ Identyfikator")[0].strip(),
            "date_added": _parse_bip_date(date_raw),
            "pos": m.start(),
            "end": seg_end,
        })
    groups = [{"desc": "", "files": []} for _ in labels]
    if not labels:
        return groups
    for gi, lab in enumerate(labels):
        # opis = okno ~600 znaków przed etykietą (tytuł wystąpienia)
        win = html[max(0, lab - 600):lab]
        text = _clean(win)
        # odetnij ogony/odnośniki plików
        text = re.sub(r"\b(?:plik|pliku|dokument|załącznik|pdf|odp\.?)\b.*$", "", text, flags=re.I)
        # usuń resztki atrybutów HTML
        text = re.sub(r"\s*=\s*[\"'][^\"']*[\"']\s*", " ", text)
        # usuń ogólne nagłówki nawigacji/części wspólne
        text = re.sub(r"Biuletyn Informacji Publicznej.*?Dane podmiotu", " ", text, flags=re.S)
        text = re.sub(r"(Urząd Miejski w Szczyrku|Menu stron|Rada Miejska 2024-2029).*", " ", text, flags=re.I)
        text = re.sub(r"(Interpelacje i zapytania radnych oraz udzielone odpowiedzi).*", " ", text, re.I)
        text = re.sub(r"\s+", " ", text).strip()
        groups[gi]["desc"] = text
        nxt = labels[gi + 1] if gi + 1 < len(labels) else len(html)
        groups[gi]["files"] = [d for d in docs if lab <= d["pos"] < nxt]
    return groups


def _match_surname(surname: str) -> str:
    """Dopasowuje nazwisko (może być w dopełniaczu, np. 'Mynarskiej'/'Zielińskiej')
    do klucza config club_assignments (mianownik 'Marcjanna Mynarska')."""
    if not surname:
        return ""
    cand = _norm(surname)
    # próby mianownika: usuń końcówki dopełniacza
    variants = {cand}
    for suf, rep in [("iej", "a"), ("ego", "y"), ("iego", ""), ("ej", "a"), ("ów", "a")]:
        if cand.endswith(suf):
            variants.add(cand[: -len(suf)] + rep)
            variants.add(cand[: -len(suf)])
    for key in _CLUB_ASSIGN:
        last = _norm(key.split()[-1])
        if not last:
            continue
        for v in variants:
            if v and (v == last or last.endswith(v)
                      or SequenceMatcher(None, v, last).ratio() >= 0.85):
                return key
    # jeszcze raz po ratio na nazwisku
    best, brat = "", 0.0
    for key in _CLUB_ASSIGN:
        last = _norm(key.split()[-1])
        r = SequenceMatcher(None, cand, last).ratio()
        if r > brat:
            best, brat = key, r
    return best if brat >= 0.8 else ""


def _parse_desc(desc: str, files) -> dict:
    """Z opisu grupy wyciąga {typ, radny, data, przedmiot}."""
    out = {"typ": "", "radny": "", "data": "", "przedmiot": ""}
    low = (desc or "").strip()
    # usuń ogólny nagłówek rejestru, który może trafić do okna
    low = re.sub(r"((?:interpelacje|interpelacji)\s+i\s+zapytania\s+radnych\s+oraz\s+udzielone\s+odpowiedzi).*", " ", low, flags=re.I)
    # typ: 'Zapytanie Radne...' vs 'Interpelacja Radne...'
    m = re.search(r"\b(Zapytanie|Interpelacja)\b\s+Radn", low, re.I)
    if m:
        out["typ"] = "zapytanie" if m.group(1).lower().startswith("zap") else "interpelacja"
    else:
        head = low[:60].lower()
        out["typ"] = "zapytanie" if re.search(r"\bzapytanie\b", head) else "interpelacja"
    # data DD-MM-YYYY
    m = re.search(r"(\d{1,2})[‑\-\./](\d{1,2})[‑\-\./](20\d\d)", low)
    if m:
        out["data"] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # radny: między 'Radnej/Radnego' a datą
    m = re.search(r"(?:Radnej|Radnego|Radny|Radna)\s+([^0-9]+?)(?=\s*\d|\s*$)", low, re.I)
    if m:
        surname = re.sub(r"[\s\.:;‑-]+$", "", m.group(1)).split()[-1]
        out["radny"] = _match_surname(re.sub(r"[\.:;]", "", surname))
    # przedmiot: po 'w sprawie' / 'w spr.'
    m = re.search(r"\bw\s+spr[a-z]*\.?\s*[:]?\s*(.+)", low)
    if m:
        subj = re.sub(r"<[^>]*", "", m.group(1))          # przytnij resztki HTML
        subj = subj.split(" <")[0]
        subj = re.sub(r"\s+", " ", subj).strip(" .:;-‑")
        if subj:
            out["przedmiot"] = subj
    return out


def build_records(groups) -> list[dict]:
    records = []
    for g in groups:
        desc = g["desc"] or ""
        files = g["files"]
        d = _parse_desc(desc, files)
        # plik-treść = pierwszy nie-odpowiedź; odpowiedź = _is_answer
        tresc = None
        odp = None
        for f in files:
            if _is_answer(f["title"]):
                if odp is None:
                    odp = f
            elif tresc is None:
                tresc = f
        if tresc is None:
            continue
        title = tresc["title"]
        typ = d["typ"] or ("zapytanie" if re.search(r"\bzapyt", title.lower()) else "interpelacja")
        radny = d["radny"] or _resolve_radny([_radny_from_filename(title)])
        subj = d["przedmiot"] or _subj_from_filename(title)
        data = d["data"] or _parse_date(title) or _parse_date(desc) or tresc["date_added"]
        rok = int(data[:4]) if data else 0
        if rok and rok < MIN_ROK_DEFAULT:
            continue
        kad = "2024-2029" if rok >= 2024 else ("2018-2024" if 2018 <= rok < 2024 else "")
        data_odp = None
        if odp:
            data_odp = _parse_date(odp["title"]) or odp["date_added"]
        records.append({
            "cri": tresc["id"],
            "typ": typ,
            "rok": rok,
            "kadencja": kad,
            "radny": radny,
            "przedmiot": subj,
            "data_wplywu": data,
            "klub": _club_for_radny(radny),
            "odpowiedz_status": "Udzielono" if odp else "Nie udzielono",
            "tresc_url": tresc["url"],
            "odpowiedz_url": odp["url"] if odp else "",
            "data_odpowiedzi": data_odp or "",
            "bip_url": tresc["url"],
        })
    seen = set()
    uniq = []
    for r in sorted(records, key=lambda x: (x["data_wplywu"] or "9999", x["cri"])):
        if r["tresc_url"] in seen:
            continue
        seen.add(r["tresc_url"])
        uniq.append(r)
    return uniq


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

    html = cached_fetch_text(REGISTER_URL) if args.cache_dir else _session().get(REGISTER_URL, timeout=40).text
    groups = parse_groups(html)
    records = build_records(groups)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[szczyrk] grupy={len(groups)} rekordy={len(records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
