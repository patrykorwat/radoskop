#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Katowice.

Źródło: https://bip.katowice.eu/RadaMiasta/Radni/

Struktura BIP:
  1. Lista radnych: RadaMiasta/Radni/default.aspx?menu=657
     Linki: Radny.aspx?ido=XXX
  2. Profil radnego: RadaMiasta/Radni/Radny.aspx?ido=XXX
     Imie i nazwisko w tagu <h2>
  3. Interpelacje radnego: RadaMiasta/Radni/interpelacje.aspx?ido=XXX
     Identyfikatory dokumentow w JavaScript: var iddelement = 'NNN'
  4. Szczegoly dokumentu: RadaMiasta/dokument.aspx?idr=NNN
     Tytul w <h2>, zalaczniki PDF w linkach

UWAGA: Uruchom lokalnie.

Użycie:
    pip install requests beautifulsoup4
    python3 scrape_interpelacje.py [--output docs/interpelacje.json] [--debug]
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("Wymagany modul: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Wymagany modul: pip install beautifulsoup4")
    sys.exit(1)


# ============================================================================
# Config
# ============================================================================

BASE_URL = "https://bip.katowice.eu"
RADNI_LIST_URL = f"{BASE_URL}/RadaMiasta/Radni/default.aspx?menu=657"

KADENCJE = {
    "IX": {"label": "IX kadencja (2024\u20132029)"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://katowice.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "text/html",
}

DELAY = 0.15  # BIP is fast, keep delay low to finish in reasonable time


# ============================================================================
# Category classification
# ============================================================================

CATEGORIES = {
    "transport": ["transport", "komunikacj", "autobus", "tramwaj", "drog", "ulic", "rondo",
                  "chodnik", "przej\u015bci", "parkow", "rower", "\u015bcie\u017ck"],
    "infrastruktura": ["infrastru", "remont", "naprawa", "budow", "inwesty", "moderniz",
                       "o\u015bwietl", "kanalizacj", "wodoci\u0105g", "nawierzch", "most"],
    "bezpiecze\u0144stwo": ["bezpiecz", "stra\u017c", "policj", "monitoring", "kradzie\u017c", "wandal",
                       "przest\u0119p", "patrol"],
    "edukacja": ["szko\u0142", "edukacj", "przedszkol", "\u017c\u0142ob", "nauczyc", "kszta\u0142c",
                 "o\u015bwiat", "uczni"],
    "zdrowie": ["zdrow", "szpital", "leczni", "medyc", "lekarz", "przychodni",
                "ambulat"],
    "\u015brodowisko": ["\u015brodowisk", "ziele\u0144", "drzew", "park ", "recykl", "odpady",
                   "\u015bmieci", "klimat", "ekolog", "powietrz", "smog", "ha\u0142as"],
    "mieszkalnictwo": ["mieszka", "lokal", "zasob", "czynsz", "wsp\u00f3lnot", "kamieni",
                       "dewelop", "budynek"],
    "kultura": ["kultur", "bibliotek", "muzeum", "teatr", "koncert", "festiwal",
                "zabytek", "zabytk"],
    "sport": ["sport", "boisko", "stadion", "basen", "si\u0142owni", "hala sport",
              "rekrea"],
    "pomoc spo\u0142eczna": ["spo\u0142eczn", "pomoc", "bezdomn", "senior", "niepe\u0142nospr",
                        "opiek", "zasi\u0142k"],
    "bud\u017cet": ["bud\u017cet", "finansow", "wydatk", "dotacj", "\u015brodki", "pieni\u0105d",
               "podatk"],
    "administracja": ["administrac", "urz\u0105d", "pracowni", "regulam", "organizac",
                      "procedur", "biurokrac"],
}


def classify_category(text):
    """Klasyfikuje kategorie interpelacji na podstawie tekstu."""
    if not text:
        return "inne"
    text_lower = text.lower()
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text_lower:
                return cat
    return "inne"


# ============================================================================
# Date parsing
# ============================================================================

def parse_date(raw):
    """Konwertuje date na format YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip().rstrip("r.,")
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    return raw


# ============================================================================
# Step 1: Get councillor list
# ============================================================================

def fetch_councillor_ids(http_session, debug=False):
    """Get all councillor IDs from the BIP radni list page."""
    if debug:
        print(f"  [DEBUG] GET {RADNI_LIST_URL}")

    try:
        resp = http_session.get(RADNI_LIST_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  BLAD: Nie mozna pobrac listy radnych: {e}")
        return []

    ids = re.findall(r'ido=(\d+)', resp.text)
    unique_ids = sorted(set(ids), key=lambda x: int(x))

    if debug:
        print(f"  [DEBUG] Znaleziono {len(unique_ids)} radnych")

    return unique_ids


# ============================================================================
# Step 2: Get councillor name and interpelacja document IDs
# ============================================================================

def fetch_councillor_name(http_session, ido, debug=False):
    """Get councillor name from their profile page (second h-tag)."""
    url = f"{BASE_URL}/RadaMiasta/Radni/Radny.aspx?ido={ido}"
    if debug:
        print(f"  [DEBUG] GET {url}")

    try:
        resp = http_session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"  [DEBUG] Blad: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    h_tags = soup.find_all(re.compile(r'^h[1-4]'))

    skip_texts = {"Rada Miasta", "Redaktorzy Biuletynu", "Adres redakcji"}
    skip_keywords = ("Dy\u017cury", "Interpelacje", "Zapytania", "O\u015bwiadczenia", "Wnioski", "Odpowiedzi")

    for h in h_tags:
        text = h.get_text(strip=True)
        # Normalize non-breaking spaces
        text = text.replace('\xa0', ' ').strip()
        # Collapse multiple spaces
        text = re.sub(r'\s{2,}', ' ', text)

        if not text or text in skip_texts:
            continue
        if any(kw in text for kw in skip_keywords):
            continue
        # A councillor name: 2+ parts, only letters/spaces/hyphens
        if len(text) > 4 and " " in text and re.match(r'^[A-Za-z\u0080-\u024f\s\-]+$', text):
            return text

    return None


def fetch_interpelacje_ids(http_session, ido, debug=False):
    """Get document IDs from a councillor's interpelacje page.

    The page embeds document IDs in JavaScript:
      var iddelement = '150 884';
    The non-breaking spaces need to be stripped.
    """
    url = f"{BASE_URL}/RadaMiasta/Radni/interpelacje.aspx?ido={ido}"
    if debug:
        print(f"  [DEBUG] GET {url}")

    try:
        resp = http_session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"  [DEBUG] Blad: {e}")
        return []

    raw_ids = re.findall(r"var iddelement = '([^']+)'", resp.text)
    cleaned = []
    for raw in raw_ids:
        clean = raw.replace('\xa0', '').replace(' ', '').strip()
        if clean.isdigit():
            cleaned.append(clean)

    return cleaned


# ============================================================================
# Step 3: Fetch document details
# ============================================================================

def fetch_document_detail(http_session, doc_id, debug=False):
    """Fetch a document detail page and extract title, date, type, PDF links.

    Document pages have:
      <h2>Interpelacja RI-IX/001000 z dnia 06.03.2026r., w sprawie ...</h2>
      <a href="/Lists/Dokumenty/Attachments/NNN/filename.pdf">filename.pdf</a>
    """
    url = f"{BASE_URL}/RadaMiasta/dokument.aspx?idr={doc_id}"
    if debug:
        print(f"    [DEBUG] GET {url}")

    try:
        resp = http_session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"    [DEBUG] Blad: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find title in h2
    title = ""
    for h2 in soup.find_all("h2"):
        text = h2.get_text(strip=True)
        if text and len(text) > 10:
            title = text
            break

    if not title:
        return None

    # Parse title components
    # Pattern: "Interpelacja RI-IX/001000 z dnia 06.03.2026r., w sprawie ..."
    typ = "interpelacja"
    title_lower = title.lower()
    if "wniosek" in title_lower:
        typ = "wniosek"
    elif "zapytanie" in title_lower:
        typ = "zapytanie"

    # Extract reference number
    nr_match = re.search(r'(RI-[IXV]+/\d+)', title)
    nr_sprawy = nr_match.group(1) if nr_match else ""

    # Extract date
    date_match = re.search(r'z dnia (\d{2}\.\d{2}\.\d{4})', title)
    data_wplywu = parse_date(date_match.group(1)) if date_match else ""

    # Extract subject (everything after "w sprawie" or after the date/comma)
    przedmiot = title
    subj_match = re.search(r'[,.]?\s*(?:w sprawie\s+)(.+)', title, re.IGNORECASE)
    if subj_match:
        przedmiot = subj_match.group(1).strip()
    else:
        # Remove the type and reference prefix
        przedmiot = re.sub(r'^(?:Interpelacja|Wniosek|Zapytanie)\s+RI-[IXV]+/\d+\s+z dnia\s+\d{2}\.\d{2}\.\d{4}r?\.,?\s*', '', title).strip()

    # Find PDF links
    tresc_url = ""
    odpowiedz_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        full_url = href if href.startswith("http") else BASE_URL + href
        link_text = a.get_text(strip=True).lower()

        if "odpowied" in link_text:
            odpowiedz_url = full_url
        elif not tresc_url:
            tresc_url = full_url

    return {
        "doc_id": doc_id,
        "typ": typ,
        "nr_sprawy": nr_sprawy,
        "data_wplywu": data_wplywu,
        "przedmiot": przedmiot,
        "full_title": title,
        "bip_url": url,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
    }


# ============================================================================
# Step 4: Fetch response links via headless browser
# ============================================================================
# BIP Katowice stores response documents as separate SharePoint items linked
# via the Dokumenty_powiazania_zew list (IDRodzic -> IDDziecko). This data is
# loaded client-side by a JavaScript function (dokumentyPowiazane) that calls
# the SharePoint SOAP API. The SOAP API requires NTLM authentication, so we
# cannot call it from Python directly. Instead we use a headless browser to
# render the document pages and extract the response links from the DOM.

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def _parse_response_html(html):
    """Extract response info from the dokumentypowiazane div HTML."""
    results = []
    for m in re.finditer(r'id="elid(\d+)"', html):
        child_id = m.group(1)
        start = m.start()
        next_m = re.search(r'id="elid\d+"', html[m.end():])
        end = m.end() + next_m.start() if next_m else len(html)
        block = html[start:end]

        pdf_match = re.search(r'href="([^"]*\.pdf[^"]*)"', block)
        pdf_url = pdf_match.group(1) if pdf_match else ""

        intro_match = re.search(r'Wprowadzenie:.*?(\d{4}-\d{2}-\d{2})', block)
        intro_date = intro_match.group(1) if intro_match else ""

        results.append({
            "child_id": child_id,
            "pdf_url": pdf_url,
            "date": intro_date,
        })
    return results


def fetch_responses_browser(records, debug=False):
    """Fetch response data for all records using a headless browser.

    Updates records in place with odpowiedz_url, data_odpowiedzi, and
    odpowiedz_status fields. Returns the updated records list.
    """
    if not HAS_PLAYWRIGHT:
        print("  UWAGA: playwright nie zainstalowany, pomijam pobieranie odpowiedzi")
        print("  Zainstaluj: pip install playwright && python3 -m playwright install chromium")
        return records

    # Collect unique doc_ids that still need response data
    need_response = {}
    for r in records:
        doc_id = r["doc_id"]
        if not r.get("odpowiedz_url") and doc_id not in need_response:
            need_response[doc_id] = True

    all_doc_ids = list(need_response.keys())
    print(f"  Dokumentow do sprawdzenia: {len(all_doc_ids)}")

    responses_map = {}
    t0 = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for i, doc_id in enumerate(all_doc_ids):
            url = f"{BASE_URL}/RadaMiasta/dokument.aspx?idr={doc_id}"
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)

                powiazane = page.query_selector(".dokumentypowiazane")
                if powiazane:
                    html = powiazane.inner_html()
                    if html and html.strip():
                        parsed = _parse_response_html(html)
                        if parsed:
                            responses_map[doc_id] = parsed
            except Exception as e:
                if debug:
                    print(f"  [DEBUG] Blad dla {doc_id}: {e}")

            if (i + 1) % 100 == 0 or (i + 1) == len(all_doc_ids):
                elapsed = time.time() - t0
                found = len(responses_map)
                print(f"  Sprawdzono: {i+1}/{len(all_doc_ids)} ({found} z odpowiedziami)")

        browser.close()

    # Merge response data into records
    updated = 0
    for r in records:
        doc_id = r["doc_id"]
        if doc_id in responses_map and not r.get("odpowiedz_url"):
            resps = responses_map[doc_id]
            best = next((x for x in resps if x["pdf_url"]), resps[0]) if resps else None
            if best:
                if best["pdf_url"]:
                    r["odpowiedz_url"] = best["pdf_url"]
                if best["date"]:
                    r["data_odpowiedzi"] = best["date"]
                    r["odpowiedz_status"] = "odpowiedziano"
                updated += 1

    print(f"  Zaktualizowano {updated} rekordow z danymi odpowiedzi")
    return records


# ============================================================================
# Main
# ============================================================================

def _load_previous_responses(output_path):
    """Wczytuje doc_id \u2192 {odpowiedz_url, data_odpowiedzi, odpowiedz_status}
    z poprzedniego interpelacje.json, je\u015bli istnieje. U\u017cywane do skipowania
    Phase [4/4] dla doc_id kt\u00f3re ju\u017c maj\u0105 znan\u0105 odpowied\u017a.

    Bez tego cache'a phase 4 robi 1028 sekwencyjnych Playwright fetchy =
    ~50min na ka\u017cdym runie. Z cachem nowy run sprawdza tylko \u015bwie\u017ce doc_id.
    """
    if not output_path or not os.path.exists(output_path):
        return {}
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            previous = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for r in previous if isinstance(previous, list) else []:
        doc_id = r.get("doc_id")
        url = r.get("odpowiedz_url")
        if doc_id and url:
            out[doc_id] = {
                "odpowiedz_url": url,
                "data_odpowiedzi": r.get("data_odpowiedzi", ""),
                "odpowiedz_status": r.get("odpowiedz_status", "udzielono odpowiedzi"),
            }
    return out


def scrape(output_path, debug=False):
    """Glowna funkcja scrapowania."""
    http_session = requests.Session()

    print("\n=== Radoskop Katowice \u2014 Scraper interpelacji ===")

    # Za\u0142aduj cache odpowiedzi z poprzedniego runu (key: doc_id).
    # Phase 4 u\u017cyje tego do skipowania znanych odpowiedzi.
    previous_responses = _load_previous_responses(output_path)
    if previous_responses:
        print(f"  Cache odpowiedzi: {len(previous_responses)} doc_id z poprzedniego runu")

    kad_name = "IX"
    print(f"\n=== {KADENCJE[kad_name]['label']} ===")

    # Step 1: Get councillor list
    print(f"\n[1/4] Pobieranie listy radnych...")
    councillor_ids = fetch_councillor_ids(http_session, debug=debug)
    if not councillor_ids:
        print("  BLAD: Nie znaleziono radnych")
        sys.exit(1)
    print(f"  Znaleziono: {len(councillor_ids)} radnych")

    # Step 2: For each councillor, get name and interpelacja document IDs
    print(f"\n[2/4] Pobieranie interpelacji radnych...")
    councillor_docs = {}  # ido -> {name, doc_ids}
    all_doc_ids = set()
    doc_to_councillors = {}  # doc_id -> [councillor_names]

    for i, ido in enumerate(councillor_ids):
        name = fetch_councillor_name(http_session, ido, debug=debug)
        time.sleep(DELAY * 0.3)

        doc_ids = fetch_interpelacje_ids(http_session, ido, debug=debug)
        time.sleep(DELAY * 0.3)

        if name:
            councillor_docs[ido] = {"name": name, "doc_ids": doc_ids}
            for did in doc_ids:
                all_doc_ids.add(did)
                if did not in doc_to_councillors:
                    doc_to_councillors[did] = []
                doc_to_councillors[did].append(name)

            if doc_ids:
                print(f"  [{i+1}/{len(councillor_ids)}] {name}: {len(doc_ids)} interpelacji")
        else:
            if debug:
                print(f"  [{i+1}/{len(councillor_ids)}] ido={ido}: brak nazwy")

    print(f"  Razem: {len(all_doc_ids)} unikalnych dokumentow")

    if not all_doc_ids:
        print("  UWAGA: Brak interpelacji do pobrania")
        # Save empty result
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return

    # Step 3: Fetch document details
    print(f"\n[3/4] Pobieranie szczegolow ({len(all_doc_ids)} dokumentow)...")
    all_records = []
    sorted_doc_ids = sorted(all_doc_ids, key=int, reverse=True)  # newest first

    for i, doc_id in enumerate(sorted_doc_ids):
        detail = fetch_document_detail(http_session, doc_id, debug=debug)

        if detail:
            # Assign councillor names from the mapping
            councillors = doc_to_councillors.get(doc_id, [])
            detail["radny"] = "\n".join(councillors) if councillors else ""
            detail["kadencja"] = kad_name
            detail["kategoria"] = classify_category(detail.get("przedmiot", ""))

            # Determine response status
            if detail.get("odpowiedz_url"):
                detail["odpowiedz_status"] = "udzielono odpowiedzi"
                detail["data_odpowiedzi"] = ""
            else:
                detail["odpowiedz_status"] = "oczekuje na odpowied\u017a"
                detail["data_odpowiedzi"] = ""

            all_records.append(detail)

        if (i + 1) % 100 == 0 or (i + 1) == len(sorted_doc_ids):
            print(f"  Pobrano: {i+1}/{len(sorted_doc_ids)} ({len(all_records)} z danymi)")

        time.sleep(DELAY)

    # Pre-fill odpowiedz_url z cache'a poprzedniego runu. fetch_responses_browser
    # ma już skip dla records z odpowiedz_url, więc to wystarczy żeby phase 4
    # iterowała tylko po nowych/niesprawdzonych doc_id.
    cached_count = 0
    for r in all_records:
        prev = previous_responses.get(r.get("doc_id"))
        if prev and not r.get("odpowiedz_url"):
            r["odpowiedz_url"] = prev["odpowiedz_url"]
            r["data_odpowiedzi"] = prev.get("data_odpowiedzi", "")
            r["odpowiedz_status"] = prev.get("odpowiedz_status", "udzielono odpowiedzi")
            cached_count += 1
    if cached_count:
        print(f"  Z cache'a: {cached_count} odpowiedzi pominiętych w phase 4")

    # Step 4: Fetch response links via headless browser (SOAP API requires NTLM)
    print(f"\n[4/4] Pobieranie odpowiedzi (headless browser)...")
    all_records = fetch_responses_browser(all_records, debug=debug)

    # Sort by date (newest first)
    all_records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Stats
    interp = sum(1 for r in all_records if r.get("typ") == "interpelacja")
    wniosek = sum(1 for r in all_records if r.get("typ") == "wniosek")
    zap = sum(1 for r in all_records if r.get("typ") == "zapytanie")
    answered = sum(1 for r in all_records if r.get("odpowiedz_url"))

    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Wnioski:      {wniosek}")
    print(f"Zapytania:    {zap}")
    print(f"Z odpowiedzi\u0105: {answered}")
    print(f"Razem:        {len(all_records)}")

    # Councillor stats
    radny_counts = {}
    for r in all_records:
        for name in r.get("radny", "").split("\n"):
            name = name.strip()
            if name:
                radny_counts[name] = radny_counts.get(name, 0) + 1

    print(f"\nRadni z interpelacjami: {len(radny_counts)}")
    for name in sorted(radny_counts, key=radny_counts.get, reverse=True)[:5]:
        print(f"  {name}: {radny_counts[name]}")

    # Save
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapyta\u0144 radnych z BIP Katowice"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Sciezka do pliku wyjsciowego (domyslnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Wlacz szczegolowe logowanie"
    )
    args = parser.parse_args()

    scrape(
        output_path=args.output,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
