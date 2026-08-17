#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Katowice (IX kadencja).

Źródło: BIP Katowice — rejestr interpelacji radnych prowadzony na SharePoint.

    https://bip.katowice.eu/RadaMiasta/Radni/

Struktura BIP Katowice (SharePoint, NIE CCT jak w Przemyślu):
  1. Lista radnych:  Radni/default.aspx?menu=657
     Każda blok `.radny` → <a href="radny.aspx?ido=X">Imię Nazwisko</a>
  2. Rejestr interpelacji radnego: Radni/interpelacje.aspx?ido=X
     Identyfikatory dokumentów w JS: `var iddelement = '153 419';`
     (non-breaking space między grupami cyfr, trzeba go usunąć).
  3. Szczegóły dokumentu: dokument.aspx?idr=Y
     Nagłówek <h2>: "Interpelacja RI-IX/001386 z dnia 03.08.2026r., w sprawie ..."
     Załączniki PDF: <a href="/Lists/Dokumenty/Attachments/Y/plik.pdf">...
  4. Odpowiedzi (Udzielono/Nie udzielono): BIP ładuje je klient-side przez SOAP
     (lista „Dokumenty_powiazania_zew", relacja IDRodzic=interpelacja,
     IDDziecko=odpowiedź, IDTablica=1). SOAP (`/_vti_bin/lists.asmx`) zwraca 401
     (wymaga NTLM), ale ten sam podgląd działa ANONIMOWO przez SharePoint REST:
       /_api/web/lists/getbytitle('Dokumenty_powiazania_zew')/items?$filter=IDRodzic eq Y and IDTablica eq 1
     → dzieci (IDDziecko). Datę odpowiedzi bierzemy z listy „Dokumenty"
     (pole Data_dokumentu) dla każdego IDDziecko.

Klub radnego brany z config.json (club_assignments -> clubs[code]['name']);
config Katowice nie ma pól `name` w sekcji clubs, więc scraper używa localnego
fallbacku (jednoznaczne, znane nazwy klubów RM Katowice: Koalicja Obywatelska,
Prawo i Sprawiedliwość, Forum Samorządowe, Niezrzeszeni).

Output: lista rekordów w formacie Radoskop (ten sam schemat co Przemyśl/
Warszawa): {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /path/cache
    python3 scrape_interpelacje.py --output docs/interpelacje.json --all   # także starsze kadencje (<2024)
    python3 scrape_interpelacje.py --output ... --max-councillors 2 --limit-docs 10   # szybki test
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache, cached_fetch_text, cached_fetch_json  # noqa: E402

BIP_BASE = "https://bip.katowice.eu"
RADNI_LIST_URL = f"{BIP_BASE}/RadaMiasta/Radni/default.aspx?menu=657"
# Listy SharePoint (bazowe URL-e REST, bez filtra — filtrujemy w kodzie).
LIST_DOKUMENTY = "Dokumenty"
LIST_POWIAZANIA = "Dokumenty_powiazania_zew"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
JSON_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json;odata=verbose",
}

DELAY = 0.5
MIN_ROK_DEFAULT = 2024  # tylko IX kadencja (2024-2029); starsze przez --all

# Konfigur Katowice nie ma pól `name` w sekcji clubs → localny fallback pełnych
# nazw klubów RM Katowice. Używany DOPIERO gdy config nie podaje nazwy.
CLUB_NAME_FALLBACK = {
    "KO": "Koalicja Obywatelska",
    "PiS": "Prawo i Sprawiedliwość",
    "Forum": "Forum Samorządowe",
    "Niezrzeszeni": "Niezrzeszeni",
}

_DEBUG = False
_session = requests.Session()
_session.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Klub
# ---------------------------------------------------------------------------

def _load_clubs() -> tuple[dict, dict]:
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    if isinstance(club, dict) and club.get("name"):
        return club["name"]
    return CLUB_NAME_FALLBACK.get(code, "")


# ---------------------------------------------------------------------------
# Radni
# ---------------------------------------------------------------------------

def parse_councillors(html: str) -> dict[str, str]:
    """{ido: 'Imię Nazwisko'} z listy radnych (bloki .radny)."""
    out: dict[str, str] = {}
    for m in re.finditer(
        r'<a href="[^"]*radny\.aspx\?ido=(\d+)[^"]*"[^>]*>(.*?)</a>', html, re.S
    ):
        ido = m.group(1)
        name = re.sub(r"<[^>]+>", " ", m.group(2))
        name = re.sub(r"\s+", " ", name).replace("\xa0", " ").strip()
        if ido and name:
            out[ido] = name
    return out


# ---------------------------------------------------------------------------
# Interpelacje radnego (rejestr)
# ---------------------------------------------------------------------------

def parse_interpelacje_ids(html: str) -> list[str]:
    """Identyfikatory dokumentów (iddelement) z rejestru radnego."""
    out: list[str] = []
    for m in re.finditer(r"var\s+iddelement\s*=\s*'([^']+)'", html):
        clean = re.sub(r"\s+", "", m.group(1))  # usuwa NBSP między grupami cyfr
        if clean.isdigit():
            out.append(clean)
    # dedupe, zachowaj kolejność
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ---------------------------------------------------------------------------
# Szczegóły dokumentu
# ---------------------------------------------------------------------------

def normalize_date(raw: str) -> str:
    """DD-MM-RRRR | DD.MM.RRRR | DD.MM.RR(??) -> RRRR-MM-DD."""
    if not raw:
        return ""
    m = re.search(r"(\d{1,2})[-.](\d{1,2})[-.](\d{3,4})", raw)
    if not m:
        return ""
    d, mo, y = m.groups()
    if len(y) == 3:  # np. "30.06.026" — literówka w źródłowym tytule BIP
        y = "2" + y
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_document_detail(
    html: str, bip_url: str, radny: str, data_dokumentu: str = ""
) -> dict | None:
    """Rekord Radoskop z dokument.aspx?idr=Y (nagłówek h2 + załączniki PDF).

    `data_dokumentu` (REST, Data_dokumentu) jest autorytatywnym źródłem daty
    wpływu — używany gdy tytuł w HTML nie zawiera poprawnej daty (literówki
    typu "a dnia" / "30.06.026" w tytułach BIP).
    """
    if not html:
        return None
    header_m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.S)
    if not header_m:
        return None
    title = re.sub(r"<[^>]+>", " ", header_m.group(1))
    title = re.sub(r"\s+", " ", title).replace("\xa0", " ").strip()
    if not title:
        return None

    # cri: Interpelacja RI-IX/001386 ...
    cri_m = re.search(r"(RI-?[IVX]+/\d+)", title)
    cri = cri_m.group(1) if cri_m else ""

    # data złożenia: "z dnia 03.08.2026r." (BIP bywa z literówką "a dnia")
    date_m = re.search(r"(?:z|a)\s+dnia\s+(\d{1,2}[-.]\d{1,2}[-.]\d{3,4})", title, re.I)
    data_wplywu = normalize_date(date_m.group(1)) if date_m else ""

    # typ: zapytanie vs interpelacja (wniosek traktujemy jak interpelację)
    low = title.lower()
    typ = "zapytanie" if "zapytanie" in low else "interpelacja"

    # przedmiot: wszystko po "w sprawie" (albo po wycięciu prefiksu)
    przedmiot = title
    subj = re.search(r"(?:w sprawie|w sprawach)\s+(.+)", title, re.IGNORECASE)
    if not subj:
        # przedmiot zaczyna się po "w sprawie"/"ws." — czasem BIP pisze "ws."
        subj = re.search(r"\bws\.\s+(.+)", title, re.IGNORECASE)
    if subj:
        przedmiot = subj.group(1).strip()
    else:
        przedmiot = re.sub(
            r"^(?:Interpelacja|Wniosek|Zapytanie)\b.*?r?\.,?\s*(?:w sprawie\s+)?",
            "",
            przedmiot,
            count=1,
        ).strip()

    # Autorytatywna data wpływu: REST Data_dokumentu (czyste ISO) > tytuł HTML.
    if (not data_wplywu) and data_dokumentu:
        data_wplywu = (data_dokumentu or "")[:10]
    if not data_wplywu:
        # fallback: data w tytule "Rok 2027 r." itp. — zostawiamy puste
        pass

    rok = 0
    if data_wplywu:
        try:
            rok = int(data_wplywu[:4])
        except (ValueError, TypeError):
            rok = 0
    kadencja = "2024-2029" if rok >= 2024 else "2018-2024"

    # załączniki PDF
    tresc_url = ""
    for href, label in re.findall(
        r'<a[^>]+href="([^"]*\.pdf[^"]*)"[^>]*>(.*?)</a>', html, re.S
    ):
        if not href.startswith("http"):
            href = BIP_BASE + href
        lowl = label.lower()
        if "odpowied" in lowl:
            continue  # odpowiedź to osobny dokument (child), link wychwycony z REST
        if not tresc_url:
            tresc_url = href

    return {
        "cri": cri,
        "typ": typ,
        "rok": rok,
        "kadencja": kadencja,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "klub": _club_for_radny(radny),
        "odpowiedz_status": "Nie udzielono",  # nadpisane w rozwiązywaniu odpowiedzi
        "tresc_url": tresc_url,
        "odpowiedz_url": "",
        "data_odpowiedzi": "",
        "bip_url": bip_url,
        "_doc_id": "",  # wewnętrzne, usuwane przed zapisem
    }


# ---------------------------------------------------------------------------
# Odpowiedzi przez SharePoint REST (anonymous)
# ---------------------------------------------------------------------------

def _rest_select(collection: str, odata_filter: str, select: str) -> list[dict]:
    """Jedna strona (≤5000) elementów listy SharePoint przez REST."""
    url = (
        f"{BIP_BASE}/_api/web/lists/getbytitle('{collection}')/items"
        f"?$filter={odata_filter}&$select={select}"
    )
    data = cached_fetch_json(url, headers=JSON_HEADERS, delay=DELAY)
    return (data.get("d") or {}).get("results", []) or []


def resolve_responses(doc_ids: list[str]) -> dict[str, dict]:
    """{doc_id: {odpowiedz_url, data_odpowiedzi}} dla interpelacji z odpowiedzią.

    Łączy relację Dokumenty_powiazania_zew (IDRodzic=interpelacja, IDTablica=1,
    IDDziecko=odpowiedź) z datą odpowiedzi z listy Dokumenty (Data_dokumentu).
    """
    if not doc_ids:
        return {}
    result: dict[str, dict] = {}
    # Pakujemy filtry (IDRodzic eq X1 or ...) i (IDTablica eq 1) w paczki po 100.
    for i in range(0, len(doc_ids), 100):
        chunk = doc_ids[i : i + 100]
        ors = " or ".join(f"IDRodzic eq {x}" for x in chunk)
        f = f"({ors}) and IDTablica eq 1"
        try:
            rows = _rest_select(LIST_POWIAZANIA, f, "ID,IDRodzic,IDDziecko")
        except Exception as e:  # noqa: BLE001
            if _DEBUG:
                print(f"  [debug] response lookup chunk {i}: {e}")
            continue
        child_ids = [str(r.get("IDDziecko")) for r in rows if r.get("IDDziecko")]
        child_meta = {}  # child_id -> {Data_dokumentu, Tytul}
        if child_ids:
            cf = " or ".join(f"ID eq {c}" for c in child_ids)
            try:
                crows = _rest_select(LIST_DOKUMENTY, cf, "ID,Tytul,Data_dokumentu")
                for r in crows:
                    child_meta[str(r.get("ID"))] = r
            except Exception:  # noqa: BLE001
                child_meta = {}
        for r in rows:
            parent = str(r.get("IDRodzic"))
            child = str(r.get("IDDziecko"))
            if not child:
                continue
            meta = child_meta.get(child, {})
            data_odp = (meta.get("Data_dokumentu") or "")[:10]
            result[parent] = {
                "odpowiedz_url": f"{BIP_BASE}/RadaMiasta/dokument.aspx?idr={child}",
                "data_odpowiedzi": data_odp,
            }
    return result


def fetch_documents_meta(doc_ids: list[str]) -> dict[str, dict]:
    """{doc_id: {'Data_dokumentu', 'Tytul'}} z listy Dokumenty (REST, batch).

    Data_dokumentu to autorytatywna data wpływu dokumentu (czyste ISO UTC),
    odporna na literówki w tytułach HTML.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(doc_ids), 100):
        chunk = doc_ids[i : i + 100]
        f = " or ".join(f"ID eq {x}" for x in chunk)
        try:
            rows = _rest_select(LIST_DOKUMENTY, f, "ID,Tytul,Data_dokumentu")
        except Exception as e:  # noqa: BLE001
            if _DEBUG:
                print(f"  [debug] meta lookup chunk {i}: {e}")
            continue
        for r in rows:
            out[str(r.get("ID"))] = r
    return out


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_councillors() -> dict[str, str]:
    html = cached_fetch_text(RADNI_LIST_URL, headers=HEADERS, delay=DELAY)
    return parse_councillors(html)


def fetch_interpelacje_page(ido: str) -> str:
    url = f"{BIP_BASE}/RadaMiasta/Radni/interpelacje.aspx?ido={ido}"
    return cached_fetch_text(url, headers=HEADERS, delay=DELAY)


def fetch_document(doc_id: str) -> str:
    url = f"{BIP_BASE}/RadaMiasta/dokument.aspx?idr={doc_id}"
    return cached_fetch_text(url, headers=HEADERS, delay=DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Katowice"
    )
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--all", action="store_true",
        help="Scrapuj też starsze kadencje (rok < 2024); domyślnie tylko 2024+",
    )
    parser.add_argument(
        "--max-councillors", type=int, default=None,
        help="Ogranicz liczbę radnych (test), domyślnie wszyscy",
    )
    parser.add_argument(
        "--limit-docs", type=int, default=None,
        help="Ogranicz liczbę dokumentów na radnego (test)",
    )
    args = parser.parse_args()
    _DEBUG = args.debug
    min_rok = None if args.all else MIN_ROK_DEFAULT

    init_cache(args.cache_dir)

    print("=== Interpelacje — BIP Katowice ===")

    councillors = fetch_councillors()
    print(f"  Radni: {len(councillors)}")

    # 1) Zbierz (doc_id -> {radny}) z rejestrów per radny.
    doc_radny: dict[str, str] = {}
    order: list[str] = []
    listed = 0
    for idx, (ido, name) in enumerate(councillors.items(), start=1):
        if args.max_councillors and idx > args.max_councillors:
            break
        html = fetch_interpelacje_page(ido)
        ids = parse_interpelacje_ids(html)
        ids = ids[: args.limit_docs] if args.limit_docs else ids
        for d in ids:
            if d not in doc_radny:
                doc_radny[d] = name
                order.append(d)
        listed += len(ids)
        if _DEBUG:
            print(f"  {idx}) {name}: {len(ids)}")
        elif idx % 5 == 0:
            print(f"  radni {idx}/{len(councillors)}... (kumulatywnie {len(doc_radny)} dok.)")
    print(f"  Rejestr: {len(doc_radny)} unikalnych dokumentów z {listed} wpisów radnych")

    # 2) Rozwiąż odpowiedzi (REST) + metadane (Data_dokumentu) dla wszystkich dokumentów.
    responses = {}
    meta = {}
    if doc_radny:
        responses = resolve_responses(order)
        meta = fetch_documents_meta(order)

    # 3) Szczegóły każdego dokumentu.
    records = []
    fetched = 0
    for doc_id in order:
        html = fetch_document(doc_id)
        data_dok = (meta.get(doc_id) or {}).get("Data_dokumentu", "")
        rec = parse_document_detail(
            html,
            f"{BIP_BASE}/RadaMiasta/dokument.aspx?idr={doc_id}",
            doc_radny[doc_id],
            data_dok,
        )
        if not rec:
            if _DEBUG:
                print(f"  [skip] brak treści: {doc_id}")
            continue
        resp = responses.get(doc_id) or {}
        if resp.get("data_odpowiedzi") or resp.get("odpowiedz_url"):
            rec["odpowiedz_status"] = "Udzielono"
            rec["odpowiedz_url"] = resp.get("odpowiedz_url", "")
            rec["data_odpowiedzi"] = resp.get("data_odpowiedzi", "")
        # filtr kadencji: pomiń rekordy bez rozpoznanego roku oraz spoza kadencji
        if not rec["rok"]:
            continue
        if min_rok and rec["rok"] < min_rok:
            continue
        rec.pop("_doc_id", None)
        records.append(rec)
        fetched += 1
        if fetched % 100 == 0:
            print(f"  szczegóły: {fetched}...")

    records.sort(key=lambda r: (r["data_wplywu"] or "", r["cri"]), reverse=True)

    interp = sum(1 for r in records if r["typ"] == "interpelacja")
    zap = sum(1 for r in records if r["typ"] == "zapytanie")
    answered = sum(1 for r in records if r["odpowiedz_status"] == "Udzielono")
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje:  {interp}")
    print(f"Zapytania:     {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:         {len(records)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
