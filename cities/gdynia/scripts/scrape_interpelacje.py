#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań radnych z BIP Gdynia.

Źródło: https://bip.um.gdynia.pl/ (React SPA z REST API)

API: https://api.um.gdynia.pl/contents/

Struktura kategorii:
  1733  → "Interpelacje i zapytania obecna kadencja"
    8850 → IX kadencja
      9407 → Rok 2026
      9227 → Rok 2025
      9053 → Rok 2024

Każdy rok to kategoria z postami miesięcznymi (type: "interpellations").
Posty zawierają dane w extended_data.interpellations[]:
  - date: "12.03.2026"
  - includedPersons: "Dudziński Marek"
  - subject: {icon, url (PDF), title}
  - answer: false | {icon, url (PDF/ZIP), title}
  - responsible_person: "Magdalena Anuszek"
  - date_of_manufacture: "12.03.2026"

Użycie:
  python3 scrape_interpelacje.py [--output docs/interpelacje.json]
                                 [--kadencja IX]
                                 [--debug]
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
    print("Wymagany moduł: pip install requests")
    sys.exit(1)

# Import shared HTTP cache (sys.path manipulacja bo scraper jest pod cities/).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "scripts"))
try:
    from http_cache import init_cache  # noqa: E402
except ImportError:
    init_cache = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = "https://api.um.gdynia.pl/contents"
BIP_BASE = "https://bip.um.gdynia.pl"

# Category IDs for kadencje
# 1733 = Interpelacje i zapytania (parent category, year subcategories directly below)
# As of April 2026, BIP Gdynia stores year categories (9407, 9227, 9053) directly
# under 1733 instead of under the previous 8850 (IX kadencja) node.
KADENCJE = {
    "IX": {"cat_id": 1733, "label": "IX kadencja (2024–2029)"},
}

HEADERS = {
    "User-Agent": "Radoskop/1.0 (https://gdynia.radoskop.pl; kontakt@radoskop.pl)",
    "Accept": "application/json",
}

DELAY = 0.5


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(session, path, params=None, debug=False):
    """GET z API Gdynia, zwraca JSON."""
    url = f"{API_BASE}{path}"
    if debug:
        print(f"  [DEBUG] GET {url} params={params}")
    resp = session.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_subcategories(session, cat_id, debug=False):
    """Pobiera podkategorie danej kategorii."""
    data = api_get(session, f"/subcategories/{cat_id}", {"bip": 1}, debug=debug)
    return data.get("subcategories", {}).get("categories", [])


def get_posts(session, cat_id, debug=False):
    """Pobiera posty (artykuły) z danej kategorii."""
    data = api_get(session, f"/posts/category/{cat_id}",
                   {"limit": 1000, "basic": 1}, debug=debug)
    return data.get("posts", {}).get("contents", [])


def get_post_detail(session, post_id, debug=False):
    """Pobiera szczegóły posta z extended_data."""
    data = api_get(session, f"/posts/{post_id}", debug=debug)
    posts = data.get("posts", [])
    return posts[0] if posts else {}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_date(raw):
    """Konwertuje datę DD.MM.YYYY na YYYY-MM-DD."""
    if not raw or not raw.strip():
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    return raw


def classify_type_from_title(title):
    """Klasyfikuje typ na podstawie tytułu interpelacji."""
    t = title.lower() if title else ""
    if t.startswith("zapytanie"):
        return "zapytanie"
    return "interpelacja"


# Name aliases: BIP name → profile name (for names that can't be auto-flipped)
NAME_ALIASES = {
    "da Silva de Oliveira Marcus": "Marcus da Silva",
    "Kłopotek - Główczewska Natalia": "Natalia Kłopotek Główczewska",
}


TITLE_PATTERNS = [
    r'\s*-\s*Wiceprzewodnicz\w+ Rady Miasta',
    r'\s*-\s*Przewodnicz\w+ Rady Miasta',
    r'\s*-\s*wygaśnięcie mandatu.*',
    r'\s*-\s*Sekretarz Rady Miasta',
]
TITLE_RE = re.compile('(' + '|'.join(TITLE_PATTERNS) + r')$', re.IGNORECASE)


def strip_title(name):
    """Usuwa tytuły/funkcje i adnotacje z końca nazwiska.

    Np. 'Ubych Jakub - Wiceprzewodniczący Rady Miasta' → 'Ubych Jakub'
        'Bartoszewicz Bartosz - wygaśnięcie mandatu z dniem 14.11.2024' → 'Bartoszewicz Bartosz'
    Nie rusza myślników w nazwiskach dwuczłonowych:
        'Kłopotek - Główczewska Natalia' → bez zmian
    """
    return TITLE_RE.sub('', name).strip()


def flip_name(name):
    """Odwraca 'Nazwisko Imię' → 'Imię Nazwisko'.

    Obsługuje nazwiska z myślnikiem, np.
    'Kłopotek - Główczewska Natalia' → 'Natalia Kłopotek - Główczewska'
    'Śrubarczyk - Cichowska Mariola' → 'Mariola Śrubarczyk - Cichowska'
    """
    if not name:
        return name

    # Check alias table first
    if name in NAME_ALIASES:
        return NAME_ALIASES[name]

    parts = name.strip().split()
    if len(parts) < 2:
        return name

    # Handle 'vel' in surname
    try:
        vel_idx = [p.lower() for p in parts].index("vel")
        surname_parts = parts[: vel_idx + 2]
        first_parts = parts[vel_idx + 2 :]
        if first_parts:
            return " ".join(first_parts) + " " + " ".join(surname_parts)
        return name
    except ValueError:
        pass

    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"

    # For 3+ parts, the LAST token is the first name, everything before is surname
    # This handles: "Kłopotek - Główczewska Natalia" → "Natalia Kłopotek - Główczewska"
    # and: "Śrubarczyk - Cichowska Mariola" → "Mariola Śrubarczyk - Cichowska"
    return parts[-1] + " " + " ".join(parts[:-1])


def clean_name(raw):
    """Czyści i normalizuje nazwisko radnego.

    Pipeline: strip title → flip name → apply per-name for multi-author.
    """
    if not raw:
        return ""
    # Handle comma-separated multi-author
    if "," in raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return ", ".join(flip_name(strip_title(n)) for n in names)
    return flip_name(strip_title(raw.strip()))


def parse_interpellation(item, kadencja_name, month_url=""):
    """Parsuje pojedynczą interpelację z extended_data."""
    date_raw = item.get("date", "")
    radny = clean_name(item.get("includedPersons", "").strip())
    subject = item.get("subject", {})
    answer = item.get("answer", False)

    przedmiot = subject.get("title", "").strip() if isinstance(subject, dict) else ""
    tresc_url = subject.get("url", "") if isinstance(subject, dict) else ""

    # Type
    typ = classify_type_from_title(przedmiot)

    # Answer
    data_odpowiedzi = ""
    odpowiedz_url = ""
    odpowiedz_status = "oczekuje na odpowiedź"

    if answer and isinstance(answer, dict):
        odpowiedz_url = answer.get("url", "")
        answer_title = answer.get("title", "").lower()
        if "przedłużenie" in answer_title or "termin" in answer_title:
            odpowiedz_status = "przedłużenie terminu"
        else:
            odpowiedz_status = "udzielono odpowiedzi"
            data_odpowiedzi = parse_date(item.get("date_of_manufacture", ""))

    data_wplywu = parse_date(date_raw)
    rok = int(data_wplywu[:4]) if data_wplywu and len(data_wplywu) >= 4 else 0

    # Clean up przedmiot — strip leading "Interpelacja w sprawie" / "Zapytanie w sprawie"
    clean_przedmiot = przedmiot
    for prefix in ["Interpelacja w sprawie ", "Zapytanie w sprawie ",
                    "Interpelacja w spr. ", "Zapytanie w spr. "]:
        if clean_przedmiot.startswith(prefix):
            clean_przedmiot = clean_przedmiot[len(prefix):]
            break

    return {
        "typ": typ,
        "kadencja": kadencja_name,
        "rok": rok,
        "radny": radny,
        "przedmiot": przedmiot,
        "data_wplywu": data_wplywu,
        "data_odpowiedzi": data_odpowiedzi,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
        "odpowiedz_status": odpowiedz_status,
        "bip_url": month_url,
        "kategoria": classify_category(przedmiot),
    }


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

CATEGORIES = {
    "transport": ["transport", "komunikacj", "autobus", "tramwaj", "drog", "ulic", "rondo",
                  "chodnik", "przejści", "parkow", "rower", "ścieżk", "przystank",
                  "sygnaliz", "skrzyżow", "trolejbus"],
    "infrastruktura": ["infrastru", "remont", "naprawa", "budow", "inwesty", "moderniz",
                       "oświetl", "kanalizacj", "wodociąg", "nawierzch", "most"],
    "bezpieczeństwo": ["bezpiecz", "straż", "policj", "monitoring", "kradzież", "wandal",
                       "przestęp", "patrol"],
    "edukacja": ["szkoł", "edukacj", "przedszkol", "żłob", "nauczyc", "kształc",
                 "oświat", "uczni"],
    "zdrowie": ["zdrow", "szpital", "leczni", "medyc", "lekarz", "przychodni",
                "ambulat"],
    "środowisko": ["środowisk", "zieleń", "drzew", "park ", "recykl", "odpady",
                   "śmieci", "klimat", "ekolog", "powietrz", "smog", "hałas"],
    "mieszkalnictwo": ["mieszka", "lokal", "zasob", "czynsz", "wspólnot", "kamieni",
                       "dewelop", "budynek"],
    "kultura": ["kultur", "bibliotek", "muzeum", "teatr", "koncert", "festiwal",
                "zabytek", "zabytk"],
    "sport": ["sport", "boisko", "stadion", "basen", "siłowni", "hala sport",
              "rekrea", "plac zabaw"],
    "pomoc społeczna": ["społeczn", "pomoc", "bezdomn", "senior", "niepełnospr",
                        "opiek", "zasiłk"],
    "budżet": ["budżet", "finansow", "wydatk", "dotacj", "środki", "pieniąd",
               "podatk"],
    "administracja": ["administrac", "urzęd", "pracowni", "regulam", "organizac",
                      "procedur", "biurokrac"],
}


def classify_category(przedmiot):
    """Klasyfikuje kategorię interpelacji na podstawie przedmiotu."""
    if not przedmiot:
        return "inne"
    text = przedmiot.lower()
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text:
                return cat
    return "inne"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(kadencje, output_path, debug=False):
    """Główna funkcja scrapowania."""
    session = requests.Session()
    all_records = []

    for kad_name in kadencje:
        kad = KADENCJE.get(kad_name)
        if not kad:
            print(f"Nieznana kadencja: {kad_name}")
            continue

        cat_id = kad["cat_id"]
        print(f"\n=== {kad['label']} (cat_id={cat_id}) ===")

        # Get year subcategories
        year_cats = get_subcategories(session, cat_id, debug=debug)
        print(f"  Znaleziono {len(year_cats)} lat: {[c['name'] for c in year_cats]}")

        if not year_cats:
            # Diagnostic: try fetching posts directly in case structure changed
            print(f"  UWAGA: brak podkategorii dla cat_id={cat_id}. "
                  f"Sprawdź czy struktura BIP Gdynia nie uległa zmianie.")
            direct_posts = get_posts(session, cat_id, debug=debug)
            if direct_posts:
                print(f"  Znaleziono {len(direct_posts)} postów bezpośrednio w kategorii "
                      f"(możliwa zmiana struktury z podkategorii rocznych na flat)")

        for year_cat in year_cats:
            year_id = year_cat["id"]
            year_name = year_cat["name"]
            print(f"\n  --- {year_name} (cat_id={year_id}) ---")

            # Get monthly posts for this year
            month_posts = get_posts(session, year_id, debug=debug)
            print(f"  Znaleziono {len(month_posts)} miesięcy")
            time.sleep(DELAY)

            for month_post in month_posts:
                post_id = month_post["id"]
                month_title = month_post.get("title", "")
                month_url = month_post.get("url", "")

                # Get full post detail with interpellations
                detail = get_post_detail(session, post_id, debug=debug)
                time.sleep(DELAY)

                ext_data = detail.get("extended_data", {})
                interpellations = ext_data.get("interpellations", [])

                print(f"    {month_title}: {len(interpellations)} interpelacji")

                for item in interpellations:
                    record = parse_interpellation(item, kad_name, month_url)
                    all_records.append(record)

    # Sort by date descending
    all_records.sort(key=lambda x: x.get("data_wplywu", ""), reverse=True)

    # Stats
    interp = sum(1 for r in all_records if r["typ"] == "interpelacja")
    zap = sum(1 for r in all_records if r["typ"] == "zapytanie")
    answered = sum(1 for r in all_records if r.get("odpowiedz_url"))
    print(f"\n=== Podsumowanie ===")
    print(f"Interpelacje: {interp}")
    print(f"Zapytania:    {zap}")
    print(f"Z odpowiedzią: {answered}")
    print(f"Razem:        {len(all_records)}")

    # Save (refuse to overwrite existing data with an empty result)
    if not all_records and os.path.exists(output_path):
        existing_size = os.path.getsize(output_path)
        if existing_size > 10:
            print(f"\nUWAGA: scraper zwrócił 0 wyników, ale {output_path} "
                  f"({existing_size / 1024:.1f} KB) już istnieje. Zachowuję stare dane.")
            print("Możliwe przyczyny: zmiana struktury kategorii w BIP, "
                  "brak połączenia z API, zmiana cat_id.")
            return

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper interpelacji i zapytań radnych z BIP Gdynia"
    )
    parser.add_argument(
        "--output", default="docs/interpelacje.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/interpelacje.json)"
    )
    parser.add_argument(
        "--kadencja", default="IX",
        help="Kadencja: IX lub 'all' (domyślnie: IX)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logowanie"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Katalog cache HTML (pipeline przekazuje scratch/.cache/interp)."
    )
    args = parser.parse_args()

    if init_cache:
        init_cache(args.cache_dir)
        if args.cache_dir:
            print(f"Cache HTML interpelacji: {args.cache_dir}")

    if args.kadencja.lower() == "all":
        kadencje = list(KADENCJE.keys())
    else:
        kadencje = [k.strip() for k in args.kadencja.split(",")]

    scrape(
        kadencje=kadencje,
        output_path=args.output,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
