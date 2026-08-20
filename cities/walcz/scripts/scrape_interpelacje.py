#!/usr/bin/env python3
"""Scraper interpelacji i zapytań radnych Rady Miasta Wałcz (IX kad. 2024-2029).

Źródło: BIP Urzędu Miasta Wałcz (Joomla) — bip.walcz.pl/rada-miasta/interpelacje-zapytania-radnych

    Kategorie (kadencja IX):
      interpelacje-kadencja-ix                    -> interpelacja-nr-X-...
      odpowiedzi-do-interpelacji-kadencja-ix      -> odpowiedz-do-interpelacji-nr-X-<radny>
      zapytania-kadencja-ix                       -> zapytanie-nr-X-...   (paginacja ?start=10)
      odpowiedzi-do-zapytan-kadencja-ix           -> odpowiedz-do-zapytania-nr-X-<radny>

  Detal wystąpienia (.item-page):
      "Kiedy została złożone interpelacja: DD.MM.RRRR r." -> data_wplywu
      "Radny: <Imię Nazwisko>"                              -> radny (klub z config)
      Tytuł = przedmiot ("Interpelacja nr 1 (dot. ...)")
      Załącznik PDF (skan) -> tresc_url (task=article.downloadAttachment)
      Metryczka "Wytworzył: <radny> (Radny Miasta Wałcz)"

  Odpowiedzi: kategoriа "odpowiedzi-do-*" — artykuł tytułem "Odpowiedź do interpelacji nr X – <radny>",
      załącznik docx/pdf -> odpowiedz_url + odpowiedz_status "Udzielono" (mapowane po nr do wystąpienia).

Klub radnego z config.json (club_assignments -> clubs). Dedupe po bip_url.
Tylko aktywna kadencja (rok>=2024), --all dla starszych (archiwum pomijane).

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
"""

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from http_cache import init_cache  # noqa: E402

BASE = "https://bip.walcz.pl"
ROOT = f"{BASE}/rada-miasta/interpelacje-zapytania-radnych"
MIN_ROK_DEFAULT = 2024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}
DELAY = 0.5
_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs():
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for(radny):
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_nominative(parsed):
    """'Nazwisko Imię' / 'Imię Nazwisko' -> kanoniczny pełny klucz z config."""
    if not parsed:
        return ""
    best, best_ratio = "", 0.0
    for name in _CLUB_ASSIGN:
        ratio = SequenceMatcher(None, parsed.lower(), name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, name
    return best if best_ratio >= 0.6 else ""


def _clean(s) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            # bip.walcz.pl ma niepoprawny certyfikat SSL (chain) — verify=False
            resp = session.get(url, timeout=40, verify=False)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (403, 429):
                time.sleep(3)
                continue
        except requests.RequestException as e:
            _log(f"  błąd {url}: {e}")
            time.sleep(2)
    return ""


def _list_articles(soup, prefix):
    """Zbierz linki artykułów z kategorii (pod-prefix kategorii)."""
    out = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        label = a.get_text(" ", strip=True)
        if href.startswith(prefix) and len(label) > 3:
            full = href if href.startswith("http") else BASE + href
            if full not in out:
                out.append(full)
    return out


def _page_links(url, per=10):
    """Wszystkie strony paginowane Joomla (?start=N), strona 1 = url bazowy."""
    pages = [(url, None)]
    try:
        r = requests.get(url, timeout=40, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return pages
    starts = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]start=(\d+)", a.get("href", ""))
        if m:
            starts.add(int(m.group(1)))
    for st in sorted(starts):
        sep = "&" if "?" in url else "?"
        pages.append((f"{url}{sep}start={st}", None))
    return pages


def _nr_from_title(title):
    m = re.search(r"nr\s*(\d+)", title, re.I)
    return int(m.group(1)) if m else None


def _detail(session, url, typ):
    """Zwraca rekord z detalu wystąpienia (interpelacja/zapytanie)."""
    html = fetch_text(session, url)
    rec = {"bip_url": url, "typ": typ, "rok": 0, "kadencja": "2024-2029",
           "radny": "", "przedmiot": "", "data_wplywu": "", "klub": "",
           "odpowiedz_status": "Nie udzielono", "tresc_url": "", "odpowiedz_url": "",
           "data_odpowiedzi": "", "cri": ""}
    if not html:
        return rec
    soup = BeautifulSoup(html, "html.parser")
    item = soup.select_one(".item-page") or soup.select_one(".com-content-article") or soup
    text = _clean(item.get_text(" "))
    # tytuł/przedmiot = h1 (np. "Interpelacja nr 3 (dot. ...)")
    title_el = soup.find("h1") or soup.select_one(".item-title") or soup.select_one("h2")
    title = _clean(title_el.get_text(" ")) if title_el else ""
    if title and title.lower() not in ("szczegóły", "załączniki", "historia zmian"):
        rec["przedmiot"] = title
    # radny — pole "Radny: X" w treści detalu (zatrzymaj się przed polem Termin odpowiedzi)
    m = re.search(r"Radny\s*:\s*([^:\n]{2,50}?)(?=\s*Termin odpowiedzi|$)", text)
    if not m:
        m = re.search(r"Wytworzył\s*:\s*([^\n]{2,50}?)\s*\((?:Radn|Burmistrz)", text)
    if m:
        raw = m.group(1).strip()
        raw = raw.split("  ")[0].strip()
        matched = _match_nominative(raw)
        rec["radny"] = matched if matched else raw
        rec["klub"] = _club_for(matched) if matched else ""
    # data wpływu: "Kiedy została złożona interpelacja / złożone zapytanie: DD.MM.RRRR r."
    m = re.search(r"Kiedy została złożon[ae]\s*(?:interpelacja|zapytanie)?\s*:\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text, re.I)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        rec["data_wplywu"] = f"{yy}-{int(mm):02d}-{int(dd):02d}"
        rec["rok"] = int(yy)
    else:
        # fallback: data z metryczki "Opublikowano: DD month RRRR"
        pm = re.search(r"Opublikowano\s*:\s*(\d{1,2})[ .](\w+)[ .](\d{4})", text, re.I)
        if pm and _month(pm.group(2)):
            rec["data_wplywu"] = f"{pm.group(3)}-{_month(pm.group(2)):02d}-{int(pm.group(1)):02d}"
            rec["rok"] = int(pm.group(3))
    # tresc_url — preferuj downloadAttachment w item-page
    for a in item.find_all("a", href=True):
        h = a.get("href", "")
        if "task=article.downloadAttachment" in h:
            full = h if h.startswith("http") else BASE + h
            if rec["tresc_url"] == "":
                rec["tresc_url"] = full
    if not rec["tresc_url"]:
        for a in item.find_all("a", href=True):
            h = a.get("href", "")
            if h.lower().endswith((".pdf", ".docx", ".doc")) and "ewidencja" not in h.lower():
                rec["tresc_url"] = h if h.startswith("http") else BASE + h
                break
    # cri z numeru w tytule (np. "Interpelacja nr 3")
    nr = _nr_from_title(rec["przedmiot"] or title)
    rec["cri"] = f"{nr}" if nr else str(abs(hash(url)) % 100000)
    return rec


def _odpowiedzi_map(session, cat):
    """Odpowiedzi: listuje kategorię i zwraca {nr: {"odpowiedz_url","data","radny"}}."""
    out = {}
    pages = _page_links(f"{ROOT}/{cat}/")
    for (purl, _) in pages:
        html = fetch_text(session, purl)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            label = a.get_text(" ", strip=True)
            if f"/{cat}/" in href and len(label) > 3 and href.startswith(f"/rada-miasta"):
                full = href if href.startswith("http") else BASE + href
                m = re.search(r"nr\s*(\d+)", label, re.I)
                nr = int(m.group(1)) if m else None
                if nr is None:
                    continue
                # radny z tytułu odpowiedzi ("Odpowiedź do zapytania nr 1 – Bogusława Towalewska")
                name_m = re.search(r"–\s*([A-ZĄĆĘŁŃÓŚŹŻ][^–]{2,45})$", label.strip())
                radny_odp = ""
                if name_m:
                    cand = _match_nominative(name_m.group(1).strip())
                    radny_odp = cand if cand else ""
                oh = fetch_text(session, full)
                resp_url = ""
                data_odp = ""
                if oh:
                    osoup = BeautifulSoup(oh, "html.parser")
                    for aa in osoup.find_all("a", href=True):
                        hh = aa.get("href", "")
                        if "task=article.downloadAttachment" in hh:
                            resp_url = hh if hh.startswith("http") else BASE + hh
                            break
                    ot = _clean(osoup.get_text(" "))
                    pm = re.search(r"Opublikowano\s*:\s*(\d{1,2})[ .](\w+)[ .](\d{4})", ot, re.I)
                    if pm and _month(pm.group(2)):
                        data_odp = f"{pm.group(3)}-{_month(pm.group(2)):02d}-{int(pm.group(1)):02d}"
                    else:
                        dm2 = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", ot)
                        if dm2:
                            data_odp = f"{dm2.group(3)}-{int(dm2.group(2)):02d}-{int(dm2.group(1)):02d}"
                out[nr] = {"url": resp_url, "data": data_odp, "radny": radny_odp}
                time.sleep(DELAY)
        time.sleep(DELAY)
    return out


_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10, "listopad": 11, "grudzień": 12,
}


def _month(s):
    return _MONTHS.get(s.lower(), 0)


def main() -> int:
    global _DEBUG
    parser = argparse.ArgumentParser(description="Scraper interpelacji — Wałcz (BIP Joomla)")
    parser.add_argument("--output", default="docs/interpelacje.json")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--all", action="store_true", help="Też starsze kadencje (archiwum)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-items", type=int, default=None, help="Ogranicz liczbę detali (testy)")
    args = parser.parse_args()
    _DEBUG = args.debug
    init_cache(args.cache_dir)
    session = _session()

    print("=== Interpelacje/zapytania — Wałcz (BIP Joomla) ===")

    # 1. Odpowiedzi (mapy nr -> url/data)
    print("  ... listuję odpowiedzi do interpelacji")
    odp_interp = _odpowiedzi_map(session, "odpowiedzi-do-interpelacji-kadencja-ix")
    print(f"    odpowiedzi do interpelacji: {len(odp_interp)}")
    print("  ... listuję odpowiedzi do zapytań")
    odp_zap = _odpowiedzi_map(session, "odpowiedzi-do-zapytan-kadencja-ix")
    print(f"    odpowiedzi do zapytań: {len(odp_zap)}")

    records = []
    for cat, typ, odp_map in [
        ("interpelacje-kadencja-ix", "interpelacja", odp_interp),
        ("zapytania-kadencja-ix", "zapytanie", odp_zap),
    ]:
        pages = _page_links(f"{ROOT}/{cat}/")
        seen = set()
        for (purl, _) in pages:
            html = fetch_text(session, purl)
            if not html:
                print(f"  [skip] {purl} brak treści")
                continue
            soup = BeautifulSoup(html, "html.parser")
            links = _list_articles(soup, f"/rada-miasta/interpelacje-zapytania-radnych/{cat}/")
            for link in links:
                if link in seen:
                    continue
                seen.add(link)
                rec = _detail(session, link, typ)
                nr = _nr_from_title(rec["przedmiot"])
                if nr and nr in odp_map:
                    rec["odpowiedz_status"] = "Udzielono" if odp_map[nr]["url"] else "Nie udzielono"
                    rec["odpowiedz_url"] = odp_map[nr]["url"]
                    rec["data_odpowiedzi"] = odp_map[nr]["data"]
                    if not rec["radny"] and odp_map[nr].get("radny"):
                        rec["radny"] = odp_map[nr]["radny"]
                        rec["klub"] = _club_for(rec["radny"]) if rec["radny"] in _CLUB_ASSIGN else ""
                if args.max_items and len(records) >= args.max_items:
                    break
                records.append(rec)
                time.sleep(DELAY)
            if args.max_items and len(records) >= args.max_items:
                break

    min_rok = None if args.all else MIN_ROK_DEFAULT
    final = []
    seen_url = set()
    for r in records:
        if min_rok and r["rok"] and r["rok"] < min_rok:
            continue
        if r["bip_url"] in seen_url:
            continue
        seen_url.add(r["bip_url"])
        final.append(r)

    interp = sum(1 for r in final if r["typ"] == "interpelacja")
    zap = sum(1 for r in final if r["typ"] == "zapytanie")
    answered = sum(1 for r in final if r["odpowiedz_status"] == "Udzielono")
    no_radny = sum(1 for r in final if not r["radny"])
    print("\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp} | Zapytania: {zap} | Z odpowiedzią: {answered} | Bez radnego: {no_radny} | Razem: {len(final)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
