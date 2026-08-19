#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Łuków.

Źródło: BIP Łuków (platforma "Wrota Lubelszczyzny" — umlukow.bip.lubelskie.pl):
  * Rejestr Interpelacji (kadencja 2024-2029)  : /index.php?id=368
  * Rejestr Zapytań                             : /index.php?id=369
Rejestr = serwerowy DataTable pod `?id={id}&action=list-ajax` (JSON, paginacja
`iDisplayStart`/`iDisplayLength`). Każdy dokument ma `tresc` (tytuł), w którym
zakodowany jest radny (dopełniacz), data pisma ("z dnia DD Miesiąc RRRR") oraz
przedmiot ("w sprawie/ z prośbą o ...").

WAŻNE: rejestr id=368 zawiera ZARÓWNO interpelacje (126), jak i ODPOWIEDZI na nie
(133, często od instytucji — "Odpowiedź Zarządu Dróg Wojewódzkich ... na
interpelację radnego Pana X z dnia ..."). Łączymy każdą odpowiedź z jej zapytaniem
(wg radnego + daty) i ustawiamy odpowiedz_status / odpowiedz_url / data_odpowiedzi.

Szczegóły: strona `?id={id}&p1=szczegoly&p2={pid}` (HTTP 302 -> `...&action=details&document_id=`)
z załącznikami "Plik źródłowy" (PDF = treść pisma / odpowiedź).

Klub radnego z config.json (club_assignments -> clubs); radnego podajemy jako
KANONICZNĄ nazwę z config (mianownik), nie dopełniacz z rejestru.

Output: lista rekordów w formacie Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}
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

from http_cache import init_cache  # noqa: E402

BASE = "https://umlukow.bip.lubelskie.pl"
REJESTR = {
    368: "interpelacja",   # rejestr Interpelacji (kadencja 2024-2029)
    369: "zapytanie",      # rejestr Zapytań
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
DELAY = 0.4
DAO = 50
MIN_ROK_DEFAULT = 2024
_DEBUG = False

_MIESIACE = {  # mianownik -> numer (używany z "z dnia DD Miesiąc RRRR")
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04", "maja": "05",
    "czerwca": "06", "lipca": "07", "sierpnia": "08", "września": "09",
    "pazdziernika": "10", "października": "10", "listopada": "11", "grudnia": "12",
}

_WPLYWU_RE = re.compile(r"z\s+dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.I)
# Radny(-i) w dopełniaczu: nazwisko(-a) bezpośrednio przed frazą "w sprawie/z prośbą/..."
_RADNEGO_RE = re.compile(
    r"radn(?:ego|ej|ych|ego\s+Rady\s+Miasta\s+\w+)\s+(?:Pana\s+|Pani[ąa]\s+)?"
    r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+){0,3}?)"
    r"(?=\s+(?:w\s+sprawie|z\s+pro[śs]b[ąa]|dotycz[ąa]|w\s+zwi[ąa]zku|o\s+przeprowadzenie|o\s+podj[ęe]cie|na\s+terenie|w\s+zwi[ęe]zku))",
    re.I,
)


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


_CLUB_STOP_WORDS = {
    "razem", "ponad", "podziałami", "podzialami", "alternatywa", "przymierze",
    "koalicja", "mieszkańców", "mieszkancow", "forum", "obywatelskie", "obywatelski",
    "niezrzeszeni", "radnych", "klubu", "nowa", "wspólnota", "wspolnota", "samorządowi",
}


def _is_clubish(name: str) -> bool:
    words = set(name.lower().split())
    return bool(words & _CLUB_STOP_WORDS)


def _canonical_radny(name: str) -> str:
    """Dopasuj (dopełniacz lub mianownik) do kanonicznej nazwy radnego z config."""
    import difflib
    if not name:
        return ""
    best, best_score = "", 0.0
    rev = " ".join(reversed(name.split()))
    for key in _CLUB_ASSIGN:
        score = max(
            difflib.SequenceMatcher(None, name.lower(), key.lower()).ratio(),
            difflib.SequenceMatcher(None, rev.lower(), key.lower()).ratio(),
        )
        if score > best_score:
            best_score, best = score, key
    return best if best_score >= 0.72 else name


def _club_for_radny(canonical: str) -> str:
    code = _CLUB_ASSIGN.get(canonical, "")
    club = _CLUBS.get(code)
    if isinstance(club, dict):
        return club.get("name", "")
    return ""


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_json(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return None


def fetch_html(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)  # requests followuje 302 -> details
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _pdf_links(html: str) -> list[str]:
    out = []
    for u in re.findall(r'href="([^"]*upload/pliki/[^"]+)"', html):
        if "download=1" in u:
            continue
        if u.startswith("/"):
            u = BASE + u
        if u not in out:
            out.append(u)
    return out


def _source_pdf(html: str) -> str:
    """Pierwszy plik 'Plik źródłowy' = treść pisma/odpowiedzi."""
    for u in _pdf_links(html):
        return u
    return ""


def _parse_date(text: str) -> str:
    m = _WPLYWU_RE.search(text)
    if not m:
        return ""
    d, mo_sl, y = m.groups()
    mo = _MIESIACE.get(mo_sl.lower(), "")
    if not mo:
        return ""
    d = int(d); y = int(y)
    if 1 <= d <= 31 and 2000 <= y <= 2100:
        return f"{y:04d}-{mo}-{d:02d}"
    return ""


def _parse_question_meta(tresc: str):
    """Z tytułu dokumentu: radny(-owie) [dopełniacz], data wpływu, przedmiot.

    Wariacje w tytule:
      "radnego Pana Artura Czubaszka z dnia ... w sprawie ..."
      "radnego Rady Miasta Łuków Pana Bartłomieja Bryka w sprawie ..."
      "radnych Rady Miasta Łuków: Pana Artura Czubaszka oraz Pana Macieja Kazany z prośbą ..."
      "radnej Pani Emilii Chruściel z dnia ... w sprawie ..."
      "złożona przez radnego Rady Miasta Pana Macieja Kazana dot. ..."
      "członka Klubu ... - Pana Sebastiana Jodełki ..."   (collective/club author)
      "Klubu Radnych Razem Ponad Podziałami"              (collective, brak radnego)
    Radnych wyciągamy przez znaczniki "Pana/Panią/Pani" (zawsze tuż przed nazwiskiem),
    a na to samo przez formę dopełniacza po "radnego/radnej/radnych".
    """
    names = []
    # 1) po "Pana / Panią / Pani"
    for m in re.finditer(r"(?:Pana|Pani[ąa]?)\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+){0,2})", tresc):
        nm = m.group(1).strip()
        # pomiń serię słów typu "Rady Miasta Łuków Radnych" gdy "Pana" był fałszywy
        if nm.lower().startswith(("rady ", "miasta ", "łuków ", "klubu ", "radnych ", "członka ")):
            continue
        if nm not in names:
            names.append(nm)
    # 2) fallback: dopełniacz tuż po radnego/radnej/radnych (gdy brak "Pana")
    if not names:
        m = re.search(
            r"\bradn(?:ego|ej|ych)\s+(?:Rady\s+Miasta(?:\s+\w+){0,3}\s+)?"
            r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+){1,2}?)"
            r"(?=\s+(?:z\s+dnia|w\s+sprawie|sprawie|dot\.|dotycz|z\s+prośb|w\s+związku|o\s+przeprowadzenie|o\s+podjęcie|na\s+terenie|$))",
            tresc, re.I)
        if m:
            names.append(m.group(1).strip())

    canon = list(dict.fromkeys(_canonical_radny(n) for n in names))  # dedupe, zachowaj kolejność
    canon = [c for c in canon if c and not _is_clubish(c)]
    data_wplywu = _parse_date(tresc)
    przedmiot = tresc.strip()
    return canon, data_wplywu, przedmiot


def _answer_authors(tresc: str):
    """Radni ODPOWIEDZI do których się odnosi (dopełniacz -> kanoniczny mianownik).

    Format: "Odpowiedź [Banku ...] na interpelację [z dnia DD M R] [złożoną przez]
    radnego [Rady Miasta Łuków] [Pana] {Imię Nazwisko} [oraz Pana {Imię2 ...}] w sprawie ...".
    Data bywa między "interpelację" a "radnego", więc radnego wyciągamy niezależnie
    od daty (po znacznikach Pana/Pani + fallback po radnego/radnej/radnych).
    """
    names = []
    for mm in re.finditer(r"(?:Pana|Pani[ąa]?)\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+){0,2})", tresc):
        nm = mm.group(1).strip()
        if nm.lower().startswith(("rady ", "miasta ", "łuków ", "klubu ", "radnych ", "członka ")):
            continue
        if nm not in names:
            names.append(nm)
    if not names:
        mm = re.search(
            r"\bradn(?:ego|ej|ych)\s+(?:Rady\s+Miasta(?:\s+\w+){0,3}\s+)?"
            r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+){1,2}?)"
            r"(?=\s+(?:z\s+dnia|w\s+sprawie|sprawie|dot\.|dotycz|z\s+prośb|w\s+związku|o\s+przeprowadzenie|o\s+podjęcie|na\s+terenie|$))",
            tresc, re.I)
        if mm:
            names.append(mm.group(1).strip())
    canon = [x for x in dict.fromkeys(_canonical_radny(n) for n in names) if x and not _is_clubish(x)]
    return canon, _parse_date(tresc)


# kept for clarity — used below; date parsed via _parse_date on full answer text
def _parse_answer_ref(tresc: str):
    return _answer_authors(tresc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji i zapytań — BIP Łuków")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--all", action="store_true", help="Scrapuj też starsze (rok<2024)")
    args = parser.parse_args()
    _DEBUG = args.debug

    init_cache(args.cache_dir)
    session = _session()

    questions = []   # {cat, pid, typ, tresc, data_utw}
    answers = []     # {pid, tresc, data_utw}
    for cat, typ in REJESTR.items():
        start = 0
        while True:
            time.sleep(DELAY)
            data = fetch_json(session,
                              f"{BASE}/index.php?id={cat}&action=list-ajax"
                              f"&iDisplayStart={start}&iDisplayLength={DAO}")
            if not data:
                break
            rows = data.get("aaData") or []
            total = int(data.get("iTotalRecords") or 0)
            for r in rows:
                t = (r.get("tresc") or "").strip()
                rec = {"pid": r.get("id_dokumentu"), "typ": typ,
                       "tresc": t, "data_utw": (r.get("data_utworzenia") or "")[:10]}
                low = t.lower()
                if low.startswith("odpowiedź") or low.startswith("odpowiedz"):
                    answers.append(rec)
                elif low.startswith("zapytanie"):
                    questions.append(rec)
                elif low.startswith("interpelacj"):
                    questions.append(rec)
                else:
                    # np. "stanowisko GDDKiA w odpowiedzi na interpelację ..." -> odpowiedź
                    if "w odpowiedzi na" in low or "odpowiedzi na" in low:
                        answers.append(rec)
                    else:
                        questions.append(rec)
            start += len(rows)
            if not rows or start >= total:
                break

    print(f"  Rejestr: zapytania/interpelacje={len(questions)}, odpowiedzi={len(answers)}")

    # Fetcz szczegółów pytań (tresc_url) i odpowiedzi (odpowiedz_url + data)
    def _detail(rec):
        html = fetch_html(session, f"{BASE}/index.php?id={ {368:368,369:369}.get(rec['typ'],368) }&p1=szczegoly&p2={rec['pid']}")
        return _source_pdf(html)

    parsed = []
    for q in questions:
        rec = {"cri": "", "typ": q["typ"], "rok": 0, "kadencja": "2024-2029",
               "radny": "", "przedmiot": q["tresc"], "data_wplywu": "",
               "klub": "", "odpowiedz_status": "Nie udzielono",
               "tresc_url": "", "odpowiedz_url": "", "data_odpowiedzi": "", "bip_url": ""}
        canon, data_wplywu, przedmiot = _parse_question_meta(q["tresc"])
        rec["radny"] = ", ".join(canon) if canon else ""
        rec["data_wplywu"] = data_wplywu
        rec["przedmiot"] = przedmiot
        first = canon[0] if canon else ""
        rec["klub"] = _club_for_radny(first)
        rok = 0
        if data_wplywu:
            rok = int(data_wplywu[:4])
        elif q["data_utw"][:4].isdigit():
            rok = int(q["data_utw"][:4])
        rec["rok"] = rok
        rec["kadencja"] = "2024-2029" if rok >= 2024 else "2018-2023"
        rec["bip_url"] = f"{BASE}/index.php?id={ {368:368,369:369}.get(q['typ'],368) }&action=details&document_id={q['pid']}"
        # cri z tytułu ("nr X/YYYY") — w Łukowie zwykle brak, użyj pid
        m = re.search(r"(?:nr|N[oó]\.?)\s*(\d{1,4})(?:\s*[/.]\s*(\d{4}))?", q["tresc"], re.I)
        rec["cri"] = (m.group(1) if m else "") or q["pid"]
        rec["tresc_url"] = _detail(q)
        parsed.append((q, rec))
        if len(parsed) % 25 == 0:
            print(f"  szczegóły pytań: {len(parsed)}...")

    # Odpowiedzi -> przypisz do pytań (wg radnego + daty; ścisłe dopasowanie daty,
    # by nie attachować starszej odpowiedzi do nowej interpelacji tego samego radnego).
    for a in answers:
        ref_authors, ref_data = _parse_answer_ref(a["tresc"])
        if not ref_authors or not ref_data:
            continue
        ref_set = set(ref_authors)
        for q, rec in parsed:
            if rec["odpowiedz_status"] == "Udzielono":
                continue
            q_authors = set(rec["radny"].split(", ")) if rec["radny"] else set()
            name_match = bool(ref_set & q_authors)
            date_match = bool(ref_data and rec["data_wplywu"] and ref_data == rec["data_wplywu"])
            if name_match and date_match and rec["data_wplywu"] <= a["data_utw"]:
                rec["odpowiedz_status"] = "Udzielono"
                rec["data_odpowiedzi"] = a["data_utw"]
                rec["odpowiedz_url"] = f"{BASE}/index.php?id=368&action=details&document_id={a['pid']}"
                break

    # Sanity: odpowiedź nie może być wcześniejsza niż wpływu (gdy obie daty są)
    for q, rec in parsed:
        if rec["odpowiedz_status"] == "Udzielono" and rec["data_odpowiedzi"] and rec["data_wplywu"] \
                and rec["data_odpowiedzi"] < rec["data_wplywu"]:
            rec["odpowiedz_status"] = "Nie udzielono"
            rec["odpowiedz_url"] = ""
            rec["data_odpowiedzi"] = ""

    # Filtry
    min_rok = None if args.all else MIN_ROK_DEFAULT
    out = []
    for q, rec in parsed:
        if not rec["rok"]:
            continue  # odrzucamy rok=0
        if min_rok and rec["rok"] < min_rok:
            continue
        out.append(rec)

    out.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in out if r["typ"] == "interpelacja")
    zap = sum(1 for r in out if r["typ"] == "zapytanie")
    answered = sum(1 for r in out if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(out)}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out_path} ({out_path.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
