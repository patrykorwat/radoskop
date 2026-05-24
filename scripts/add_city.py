#!/usr/bin/env python3
"""
add_city.py — masowe dodawanie miast do Radoskopu.

Dla każdego slug:
  1. Probe https://{slug}.esesja.pl/ — czy domena działa (Logonet eSesja).
  2. Fetch składu rady z PKW (Państwowa Komisja Wyborcza) — oficjalne dane
     rządowe z wyborów samorządowych 2024-04-07. CSV
     samorzad2024.pkw.gov.pl/data/csv/kandydaci_rady_gmin_*.zip zawierają
     wszystkie wybrane radnych (flaga "Czy uzyskał mandat" = Tak) plus
     skrót komitetu wyborczego (KW PRAWO I SPRAWIEDLIWOŚĆ, KKW KOALICJA
     OBYWATELSKA, KWW lokalne komitety). Fallback: parsing eSesja landing
     gdy slug nie matchuje PKW.
  3. Generuje radoskop/cities/{slug}/config.json + scripts/scrape_{slug}.py
     (thin wrapper na lib_esesja jeśli eSesja działa, albo stub).
  4. Smoke test --dry-run i sprawdza ile sesji w aktualnej kadencji.
  5. Opcjonalnie: dopisuje slug do radoskop/data/cities-meta.csv (jeśli
     brak) plus radoskop-premium/scrape_all.sh ALL_CITIES + nas/run_pipeline.py.

Status per slug: OK | NO_ESESJA | ESESJA_EMPTY | NO_COMPOSITION | DRY_RUN_FAILED.

Wymagane: requests, beautifulsoup4 (już w pipeline radoskopu).

Użycie:
    # Pojedyncze miasto:
    python3 add_city.py opole

    # Wsadowo z CSV (jeden slug per linia, opcjonalnie 'slug,name'):
    python3 add_city.py --batch cities-todo.csv

    # Tylko sprawdź dostępność eSesja (nie generuj):
    python3 add_city.py --probe-only opole tarnow sosnowiec

    # Dopisz do pipeline (scrape_all.sh + run_pipeline.py):
    python3 add_city.py opole --register

    # Dry run (nic nie zapisuje na dysku):
    python3 add_city.py opole --no-write
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install requests beautifulsoup4 lxml", file=sys.stderr)
    raise

# Slownik genitive — OVERRIDES dla ~500 miast plus heurystyka morfologiczna.
# Wczesniej był wbudowany _polish_genitive w tym pliku, ale po batchu 112 miast
# 2026-05-17 okazało się że zbyt wąski słownik daje błędne formy
# (Bełchatów → Bełchatowa OK, ale rzadsze miasta jak Pieniężno → Pieniężna FAIL).
# Wyodrebniony do pl_genitive.py żeby można było rozszerzać/poprawiać bez
# dotykania logiki add_city.py.
from pl_genitive import genitive as _polish_genitive


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Repo layout — wykrywane od miejsca skryptu.
SCRIPT_DIR = Path(__file__).resolve().parent
RADOSKOP_ROOT = SCRIPT_DIR.parent  # radoskop/
WORKSPACE_ROOT = RADOSKOP_ROOT.parent  # workspace zawiera radoskop/ + radoskop-premium/
PREMIUM_ROOT = WORKSPACE_ROOT / "radoskop-premium"
CITIES_DIR = RADOSKOP_ROOT / "cities"
CITIES_META_CSV = RADOSKOP_ROOT / "data" / "cities-meta.csv"
SCRAPE_ALL_SH = PREMIUM_ROOT / "scrape_all.sh"
RUN_PIPELINE_PY = PREMIUM_ROOT / "nas" / "run_pipeline.py"


# ---------------------------------------------------------------------------
# Mapowanie nazw komitetów wyborczych → krótki kod + kolor + display name.
# ---------------------------------------------------------------------------

# Stałe komitety ogólnopolskie. KWW lokalne dostają auto-skrót.
CLUB_REGISTRY: list[tuple[re.Pattern, str, str, str]] = [
    # (regex_match_against_uppercase_committee_name, code, display, color)
    (re.compile(r"PRAWO\s+I\s+SPRAWIEDLIWO[ŚS][ĆC]"), "PiS",
     "Prawo i Sprawiedliwość", "#1f4ea0"),
    (re.compile(r"KOALICJA\s+OBYWATELSKA"), "KO",
     "Koalicja Obywatelska", "#ea580c"),
    (re.compile(r"^LEWICA|KKW\s+LEWICA|NOWA\s+LEWICA"), "Lewica",
     "Lewica", "#dc2626"),
    (re.compile(r"TRZECIA\s+DROGA|PSL[\s\-]+PL\s*2050|POLSKA\s*2050"), "TD",
     "Trzecia Droga", "#16a34a"),
    (re.compile(r"KONFEDERACJA"), "Konf",
     "Konfederacja", "#0f172a"),
]

NIEZRZESZENI = ("NZ", "Niezrzeszeni", "#6b7280")

# Kolory tła + avatara liczone z koloru głównego (jasniejszy bg + ciemniejszy avatar).
def _derive_palette(color_hex: str) -> tuple[str, str]:
    """Z koloru głównego → (bg rgba 12%, avatar_bg ciemniejszy o ~20%)."""
    h = color_hex.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    bg = f"rgba({r},{g},{b},0.12)"
    # Avatar = -25% lightness
    avatar_r = max(0, int(r * 0.75))
    avatar_g = max(0, int(g * 0.75))
    avatar_b = max(0, int(b * 0.75))
    avatar = f"#{avatar_r:02x}{avatar_g:02x}{avatar_b:02x}"
    return bg, avatar


def _classify_committee(raw: str) -> tuple[str, str, str]:
    """Z surowego stringu nazwy komitetu → (code, display_name, color).

    Najpierw próbuje match przeciwko CLUB_REGISTRY (PiS, KO, Lewica, TD, Konf).
    Fallback: KWW lokalne → auto-skrót z 2-4 dużych liter, display = oryginalna
    nazwa skrócona, kolor z palette rotacja.
    """
    up = raw.upper()
    for pattern, code, display, color in CLUB_REGISTRY:
        if pattern.search(up):
            return code, display, color
    # KWW lokalny — skrót auto z pierwszych liter słów oprócz "KWW"
    # np. "KWW MARCINA BAZYLAKA DĄBROWIANIE RAZEM" → "MBDR"
    clean = re.sub(r"^(KWW|KKW|KW)\s+", "", up).strip()
    words = re.findall(r"[A-ZŁŚĄĘĆŃÓŻŹ]+", clean)
    # Filtruj imiona własne (>4 słowa rzędu skrótów stają nieczytelne).
    short = [w for w in words if len(w) >= 3]
    code = "".join(w[0] for w in short[:4]) or "NZ"
    # Display: skróć do max 60 znaków, kapitalizacja
    display = raw.strip()
    if len(display) > 60:
        display = display[:57] + "..."
    # Rotacja kolorów po hash code
    palette = ["#0284c7", "#a16207", "#16a34a", "#7c3aed", "#db2777", "#0d9488"]
    color = palette[abs(hash(code)) % len(palette)]
    return code, display, color


# ---------------------------------------------------------------------------
# Probe eSesja
# ---------------------------------------------------------------------------

@dataclass
class EsesjaProbe:
    slug: str
    works: bool  # czy subdomena istnieje i nie redirectuje na landing
    title: str = ""
    sessions_count: int = 0  # liczba sesji w /glosowania (heuristyka)
    error: str = ""


def probe_esesja(slug: str, timeout: int = 15) -> EsesjaProbe:
    """Sprawdza https://{slug}.esesja.pl/ — czy działa.

    Redirect na esesja.pl/ landing oznacza że subdomena nie istnieje
    (Logonet podpinają tylko miasta-klientów do {slug}.esesja.pl).
    """
    url = f"https://{slug}.esesja.pl/"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT},
                            allow_redirects=True)
    except Exception as exc:
        return EsesjaProbe(slug=slug, works=False, error=str(exc))
    final = resp.url.rstrip("/")
    expected = url.rstrip("/")
    if final != expected:
        return EsesjaProbe(slug=slug, works=False, error=f"redirect → {final}")
    # Przekazujemy bytes — lxml sam wykryje encoding z <meta charset>.
    soup = BeautifulSoup(resp.content, "lxml")
    title = (soup.find("title").get_text(strip=True) if soup.find("title") else "")[:200]
    if "Rada" not in title or "eSesja" not in title:
        return EsesjaProbe(slug=slug, works=False, title=title,
                           error="nieoczekiwany tytuł, prawdopodobnie landing")
    return EsesjaProbe(slug=slug, works=True, title=title)


# ---------------------------------------------------------------------------
# Fetch składu z PKW (Państwowa Komisja Wyborcza) — oficjalne dane rządowe
# z wyborów samorządowych 2024-04-07.
#
# PKW publikuje CSV z wszystkimi kandydatami do rad gmin (319 powyżej 20k +
# 2114 do 20k = 2433 gmin Polski) z flagą "Czy uzyskał mandat" Tak/Nie i
# skrótem komitetu wyborczego. To kompletna baza składów rad miejskich
# wybranych w 2024 r. — nie obejmuje zmian w trakcie kadencji (rezygnacje,
# zastępcy) ale jest najbardziej autoritetywne źródło inicjalne.
# ---------------------------------------------------------------------------

PKW_BASE = "https://samorzad2024.pkw.gov.pl/samorzad2024/data/csv"
PKW_DATASETS = {
    "rady_gmin_powyzej_20k": f"{PKW_BASE}/kandydaci_rady_gmin_powyzej_20k_csv.zip",
    "rady_gmin_do_20k": f"{PKW_BASE}/kandydaci_rady_gmin_do_20k_csv.zip",
    "rady_dzielnic": f"{PKW_BASE}/kandydaci_rady_dzielnic_csv.zip",
}
PKW_CACHE_DIR = Path("/tmp/radoskop_pkw_cache")


@dataclass
class CouncilComposition:
    city_name: str = ""
    voivodeship: str = ""
    population: Optional[int] = None
    teryt: str = ""
    president_name: str = ""
    president_committee: str = ""
    # name → committee_raw (committee_raw może być "" jeśli z eSesja landing
    # — wtedy fallbackuje do NZ w build_config).
    councilors: dict[str, str] = field(default_factory=dict)
    total_seats: int = 0  # ile mandatów ogółem (z opisu)
    error: str = ""
    source: str = ""  # "pkw" | "esesja" — żeby raport pokazał skąd


# ---------------------------------------------------------------------------
# PKW dataset loader (cache w /tmp)
# ---------------------------------------------------------------------------

_PKW_CACHE: Optional[dict[str, list[dict]]] = None


def _download_pkw_dataset(key: str) -> Path:
    """Pobiera i ekstraktuje CSV dataset PKW (z cache w /tmp).

    Każdy plik to ~1-20 MB, cachowanie radykalnie skraca runtime przy batch.
    """
    PKW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = PKW_CACHE_DIR / f"{key}.csv"
    if csv_path.is_file() and csv_path.stat().st_size > 1000:
        return csv_path
    import zipfile
    zip_path = PKW_CACHE_DIR / f"{key}.zip"
    url = PKW_DATASETS[key]
    print(f"  Pobieram PKW dataset: {url}")
    resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    zip_path.write_bytes(resp.content)
    with zipfile.ZipFile(zip_path) as zf:
        # Każdy ZIP zawiera 1 CSV z nazwą zbliżoną do klucza.
        members = [n for n in zf.namelist() if n.endswith(".csv")]
        if not members:
            raise RuntimeError(f"ZIP {key} pusty")
        with zf.open(members[0]) as src, csv_path.open("wb") as dst:
            dst.write(src.read())
    return csv_path


def load_pkw_data(cities_only: bool = True) -> dict[str, list[dict]]:
    """Lazy-load + cache całego datasetu PKW dla rad gmin (>20k, do 20k).

    cities_only=True (default): pomija "Rada Gminy" (gminy wiejskie), zostawia
    "Rada Miasta" + "Rada Miejska" (gminy miejskie + miejsko-wiejskie). Daje
    ~984 jednostek zamiast 2393. Radoskop trzyma się miast — gminy wiejskie
    mają inny status prawny i często nie publikują głosowań imiennych
    w sposób parsowalny.

    Zwraca {terc → list[radny_dict]} — tylko wybranych ("Tak").
    """
    global _PKW_CACHE
    cache_key = "cities" if cities_only else "all"
    if isinstance(_PKW_CACHE, dict) and _PKW_CACHE.get("_key") == cache_key:
        return _PKW_CACHE["data"]  # type: ignore[return-value]
    by_terc: dict[str, list[dict]] = {}
    for key in ["rady_gmin_powyzej_20k", "rady_gmin_do_20k"]:
        path = _download_pkw_dataset(key)
        # utf-8-sig: PKW CSV ma BOM na początku, bez tego pierwsza nazwa
        # kolumny jest '﻿"Rada"' i DictReader nie znajduje pola.
        with path.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                if row.get("Czy uzyskał mandat", "").strip() != "Tak":
                    continue
                rada = row.get("Rada", "").strip()
                if cities_only:
                    # Tylko miasta: "Rada Miasta X" lub "Rada Miejska w X".
                    # Pomiń "Rada Gminy" (wiejskie) i "Rada m.st. Warszawy" (dzielnice).
                    if not (rada.startswith("Rada Miasta") or rada.startswith("Rada Miejska")):
                        continue
                terc = (row.get("TERYT Gminy") or "").strip()
                if not terc:
                    continue
                by_terc.setdefault(terc, []).append({
                    "name_pkw": row.get("Nazwisko i imiona", "").strip(),
                    "committee": row.get("Skrót nazwy komitetu", "").strip(),
                    "committee_full": row.get("Nazwa komitetu", "").strip(),
                    "rada": rada,
                    "gmina": row.get("Gmina", "").strip(),
                    "okreg": row.get("Nr okręgu", "").strip(),
                    "wojewodztwo_kod": terc[:2],
                })
    _PKW_CACHE = {"_key": cache_key, "data": by_terc}  # type: ignore[assignment]
    return by_terc


def _normalize_to_slug(name: str) -> str:
    """Polskie nazwy gminy → ASCII slug (np. 'Jelenia Góra' → 'jelenia-gora')."""
    name = re.sub(r"^m\.\s+", "", name)  # 'm. Bolesławiec' → 'Bolesławiec'
    name = name.lower().strip()
    # Strip diakrytyki
    nfd = unicodedata.normalize("NFD", name)
    no_diacritics = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    # Ł i Ó nie rozkładają się w NFD, mapujemy ręcznie
    table = str.maketrans({"ł": "l", "ż": "z", "ź": "z", "ó": "o"})
    no_diacritics = no_diacritics.translate(table)
    # Spacje → hyphens, drop reszty znaków
    slug = re.sub(r"[^\w\s-]", "", no_diacritics)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug


def _build_pkw_slug_index(data: dict[str, list[dict]]) -> dict[str, str]:
    """Buduje mapping slug → terc na podstawie nazw gmin w PKW."""
    out: dict[str, str] = {}
    for terc, radni in data.items():
        if not radni:
            continue
        gmina = radni[0].get("gmina", "")
        slug = _normalize_to_slug(gmina)
        if slug:
            out[slug] = terc
    return out


def fetch_composition_pkw(slug: str) -> CouncilComposition:
    """Pobiera skład rady z PKW dataset po slug-u.

    Strategia: load PKW CSV (cached), build slug→terc index, pickup radnych
    z flagą 'Czy uzyskał mandat' Tak.
    """
    comp = CouncilComposition(source="pkw")
    try:
        data = load_pkw_data()
    except Exception as exc:
        comp.error = f"PKW dataset fetch: {exc}"
        return comp
    slug_idx = _build_pkw_slug_index(data)
    terc = slug_idx.get(slug)
    if not terc:
        # Fallback: spróbuj wariantów (np. 'jelenia-gora' → 'jelenia-gora-miasto')
        for s, t in slug_idx.items():
            if s.startswith(slug) or slug in s:
                terc = t
                break
    if not terc:
        comp.error = f"slug '{slug}' nie znaleziony w PKW (sprawdzono {len(slug_idx)} gmin)"
        return comp
    radni = data[terc]
    comp.teryt = terc
    # PKW pole "Gmina" to ZAWSZE nominative z prefiksem "m. " (np. "m. Bolesławiec").
    # Wcześniej brałem z pola "Rada" przez regex, ale PKW wpisuje czasem
    # nominative ("Rada Miasta Bolesławiec") a czasem locative
    # ("Rada Miejska w Bolesławcu") — locative regex łapał "Bolesławcu" jako
    # nazwę miasta. Strip "m. " z Gmina jest bezpieczniejszy.
    comp.city_name = radni[0].get("gmina", "").replace("m. ", "", 1).strip()
    comp.total_seats = len(radni)
    for r in radni:
        # 'NOWAK Jan Adam' → 'Jan Nowak' (Imię Nazwisko, kapitalizacja)
        nazwisko_imiona = r["name_pkw"]
        parts = nazwisko_imiona.split()
        if len(parts) >= 2:
            # Heurystyka: nazwiska są ALL CAPS, imiona TitleCase
            # 'NOWAK Jan Adam' → nazwisko='NOWAK', imiona=['Jan','Adam']
            nazwisko_tokens = []
            imiona_tokens = []
            for tok in parts:
                if tok.isupper() and not imiona_tokens:
                    nazwisko_tokens.append(tok.title())
                else:
                    imiona_tokens.append(tok)
            if nazwisko_tokens and imiona_tokens:
                # 'Imię Nazwisko' format
                first_name = imiona_tokens[0]
                last_name = " ".join(nazwisko_tokens)
                full_name = f"{first_name} {last_name}"
            else:
                full_name = nazwisko_imiona.title()
        else:
            full_name = nazwisko_imiona.title()
        comp.councilors[full_name] = r["committee"]
    return comp


def fetch_composition_esesja(slug: str) -> CouncilComposition:
    """Pobiera skład rady z eSesja landing — sekcja "Radni" na stronie głównej.

    Format eSesja:
        ...
        Radni
        Sławomir Batko
        Wiceprzewodniczący Rady Miasta Opola
        Elżbieta Bień
        Radna Miasta Opola
        ...

    Nie podaje przypisań klubowych — wszyscy dostaną "" → fallback NZ
    w build_config. User dosadza ręcznie po pierwszym scrape.
    """
    comp = CouncilComposition(source="esesja")
    url = f"https://{slug}.esesja.pl/"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except Exception as exc:
        comp.error = f"fetch: {exc}"
        return comp
    soup = BeautifulSoup(resp.content, "lxml")
    # Tytuł strony zawiera "Rada Miasta {Nazwa}" (nominative) lub
    # "Rada Miejska w {Nazwa}" (locative — "w" wymaga w polskim locative).
    # Aby uniknąć złapania odmienionej formy ("w Bolesławcu" → "Bolesławcu"),
    # akceptujemy tylko nominative pattern bez "w ". Dla locative przypadku
    # nazwa miasta zostaje pusta i fallbackuje do slug.title() w build_config.
    title = soup.find("title")
    if title:
        m = re.search(r"Rada\s+(?:Miasta|Miejska|Gminy)\s+([A-ZŁŚĄĘĆŃÓŻŹ][\w\-]+(?:\s+[A-ZŁŚĄĘĆŃÓŻŹ][\w\-]+)?)\s+na\s+platformie",
                      title.get_text(strip=True))
        if m:
            comp.city_name = m.group(1).strip()
    # Sekcja "Radni" — heading h2/h3 z tekstem "Radni", potem lista linkow
    # do per-councilor stron. Każdy radny jest jako <h3>/<p>/<a> z imieniem.
    # Strategia: znajdź heading "Radni", zbierz po nim teksty wyglądające
    # na imiona (2-3 słowa, każde z dużą literą).
    body_text = soup.get_text("\n", strip=True)
    lines = [l for l in body_text.split("\n") if l.strip()]
    started = False
    for line in lines:
        if not started and line.strip() == "Radni":
            started = True
            continue
        if not started:
            continue
        # Stop conditions: stopka portalu, lub przerwa
        if "Portal informacyjny" in line or "Licznik odwiedzin" in line:
            break
        # Imię Nazwisko: 2-3 słowa, każde z dużą literą (Pl diakrytyki OK)
        if re.match(r"^[A-ZŁŚĄĘĆŃÓŻŹ][a-złśąęćńóżź\-]+(\s+[A-ZŁŚĄĘĆŃÓŻŹ][a-złśąęćńóżź\-]+){1,3}$",
                    line.strip()):
            name = _normalize_name(line.strip())
            comp.councilors.setdefault(name, "")
    return comp


def _normalize_name(name: str) -> str:
    """Imię Drugie Nazwisko → 'Imię Nazwisko' (drop middle name).

    Radoskop config trzyma format 'First Last'. Niektóre źródła podają
    pełne (Sebastian Czesław Czyżyk-Skoczyk) — bierzemy First + Last.
    """
    parts = name.split()
    if len(parts) <= 2:
        return name
    # Sklejone z myślnikami w nazwisku: ostatni token może być długi
    return f"{parts[0]} {parts[-1]}"


# ---------------------------------------------------------------------------
# Generowanie config.json + scrape_{slug}.py
# ---------------------------------------------------------------------------

def build_config(slug: str, comp: CouncilComposition, esesja_url: Optional[str]) -> dict:
    """Buduje dict gotowy do json.dump jako config.json."""
    # Klubowy roster: każdy radny dostaje code z _classify_committee
    club_assignments: dict[str, str] = {}
    clubs: dict[str, dict] = {}
    seen_codes: set[str] = set()
    for name, committee_raw in sorted(comp.councilors.items()):
        code, display, color = _classify_committee(committee_raw)
        club_assignments[name] = code
        if code not in seen_codes:
            bg, avatar_bg = _derive_palette(color)
            clubs[code] = {
                "name": display,
                "color": color,
                "bg": bg,
                "avatar_bg": avatar_bg,
            }
            seen_codes.add(code)
    # Zawsze dodaj NZ jako fallback
    if "NZ" not in clubs:
        bg, avatar_bg = _derive_palette(NIEZRZESZENI[2])
        clubs["NZ"] = {
            "name": NIEZRZESZENI[1],
            "color": NIEZRZESZENI[2],
            "bg": bg,
            "avatar_bg": avatar_bg,
        }

    city_name = comp.city_name or slug.replace("-", " ").title()
    # Genitive heuristyka: doda 'a' do nazwy męskiej, 'i' do żeńskiej, etc.
    # Prosto: zwróć name + (a) jeśli brak ostatniego znaku samogłoska, inaczej -y
    genitive = _polish_genitive(city_name)

    return {
        "city_name": city_name,
        "city_genitive": genitive,
        "site_title": f"Radoskop {city_name} — Jak głosują radni?",
        "site_url": f"https://{slug}.radoskop.pl",
        "site_description": f"Radoskop — otwarte narzędzie monitoringu Rady Miasta {genitive}. Sprawdź frekwencję, głosowania imienne i aktywność radnych.",
        "site_description_short": f"Otwarte narzędzie monitoringu Rada Miasta {genitive}.",
        "bip_url": f"https://bip.{slug}.pl/",
        "bip_name": f"BIP {city_name}",
        "esesja_url": esesja_url,
        "github_url": "https://github.com/radoskoppl/radoskop",
        "author": "Patryk Orwat",
        "cname": f"{slug}.radoskop.pl",
        "clubs": clubs,
        "club_assignments": club_assignments,
        "kadencje": {
            "2024-2029": {
                "label": "IX kadencja (2024–2029)",
                "start": "2024-05-07",
            },
        },
        "kadencja_active": "2024-2029",
        "has_budget": False,
        "has_komisje": False,
        "budget_note": "",
        "samorzad_type": "miasto",
        "rada_name": f"Rada Miasta {genitive}",
        "rada_name_genitive": f"Rady Miasta {genitive}",
    }


SCRAPER_TEMPLATE = '''#!/usr/bin/env python3
"""
Radoskop {NAME} — eSesja scraper (thin wrapper around scripts/lib_esesja.py).

Skład rady + przypisania klubowe są wczytywane z config.json (sekcja
club_assignments). Format: {{"Imię Nazwisko": "kod_klubu"}}.

Backend: {BASE_URL} (Rada Miasta {NAME}, IX kadencja 2024-2029).
Wygenerowane automatycznie przez radoskop/scripts/add_city.py.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

KADENCJE = {{
    "2024-2029": {{"label": "IX kadencja (2024–2029)", "start": "2024-05-07"}},
}}


def _load_councilors() -> dict[str, str]:
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {{}}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {{}}
    return cfg.get("club_assignments", {{}}) or {{}}


COUNCILORS = _load_councilors()


if __name__ == "__main__":
    raise SystemExit(EsesjaScraper(
        base_url="{BASE_URL}",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop {NAME} ({BASE_URL})"))
'''


def build_scraper_py(slug: str, city_name: str, esesja_url: str) -> str:
    return SCRAPER_TEMPLATE.format(NAME=city_name, BASE_URL=esesja_url.rstrip("/"))


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

@dataclass
class DryRunResult:
    slug: str
    success: bool
    sessions_total: int = 0
    sessions_in_kadencja: int = 0
    output: str = ""
    error: str = ""


def run_dry_run(slug: str) -> DryRunResult:
    """Odpala scrape_{slug}.py --dry-run i parsuje wynik."""
    script = CITIES_DIR / slug / "scripts" / f"scrape_{slug.replace('-', '_')}.py"
    if not script.is_file():
        return DryRunResult(slug=slug, success=False,
                            error=f"scraper nie istnieje: {script}")
    tmp_out = Path(f"/tmp/add_city_{slug}")
    tmp_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-u", str(script),
        "--output", str(tmp_out / "data.json"),
        "--profiles", str(tmp_out / "profiles.json"),
        "--dry-run",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return DryRunResult(slug=slug, success=False, error="timeout 180s")
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return DryRunResult(slug=slug, success=False, output=out[-500:],
                            error=f"exit {proc.returncode}")
    m_total = re.search(r"Znaleziono\s+(\d+)\s+sesji\s+ogolnie", out)
    m_kad = re.search(r"(\d+)\s+w\s+kadencji\s+", out)
    return DryRunResult(
        slug=slug,
        success=True,
        sessions_total=int(m_total.group(1)) if m_total else 0,
        sessions_in_kadencja=int(m_kad.group(1)) if m_kad else 0,
        output=out[-300:],
    )


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def write_city_files(slug: str, config: dict, scraper_code: str,
                     dry_write: bool = False) -> list[Path]:
    """Tworzy radoskop/cities/{slug}/config.json + scripts/scrape_{slug}.py."""
    city_dir = CITIES_DIR / slug
    scripts_dir = city_dir / "scripts"
    config_path = city_dir / "config.json"
    scraper_path = scripts_dir / f"scrape_{slug.replace('-', '_')}.py"

    if dry_write:
        return [config_path, scraper_path]

    scripts_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    scraper_path.write_text(scraper_code, encoding="utf-8")
    return [config_path, scraper_path]


def geocode_city(city_name: str, country: str = "pl") -> Optional[tuple[float, float]]:
    """Geocode via Photon (komoot) — szybki, public, bez API key.

    Filtruje po kraju i preferuje osm_key=place (city/town/village). Wszystkie
    miasta w cities-meta.csv muszą mieć współrzędne żeby renderować się na
    Leaflet mapie strony głównej. Wynik np. (52.2297, 21.0122) dla Warszawy.

    Country fallback to Polska. Dla zagranicznych miast w batch (Vilnius, Berlin,
    Praga) trzeba podać country='lt' / 'de' / 'cz'.
    """
    import urllib.parse, urllib.request, json as _json
    country_names = {"pl": "Polska", "cz": "Česko", "de": "Deutschland", "lt": "Lietuva"}
    country_full = country_names.get(country, "Polska")
    q = urllib.parse.quote(f"{city_name}, {country_full}")
    url = f"https://photon.komoot.io/api/?q={q}&limit=10"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Radoskop/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())
    except Exception as exc:
        print(f"    geocode err: {exc}")
        return None
    feats = data.get("features", [])
    same_country = [f for f in feats if f.get("properties", {}).get("country") == country_full]
    GOOD = {"city", "town", "municipality", "village"}
    for f in same_country:
        p = f.get("properties", {})
        if p.get("osm_key") == "place" and p.get("osm_value") in GOOD:
            lon, lat = f["geometry"]["coordinates"]
            return (lat, lon)
    for f in same_country:
        p = f.get("properties", {})
        if p.get("osm_key") == "boundary" and p.get("osm_value") == "administrative":
            lon, lat = f["geometry"]["coordinates"]
            return (lat, lon)
    if same_country:
        lon, lat = same_country[0]["geometry"]["coordinates"]
        return (lat, lon)
    return None


def append_to_cities_meta(slug: str, voivodeship: str, population: Optional[int],
                          lat: Optional[float], lon: Optional[float],
                          country: str = "pl",
                          dry_write: bool = False) -> bool:
    """Dopisuje wiersz do cities-meta.csv jeśli slug nie ma."""
    if not CITIES_META_CSV.is_file():
        return False
    existing = CITIES_META_CSV.read_text(encoding="utf-8")
    if re.search(rf"^{re.escape(slug)},", existing, re.MULTILINE):
        return False  # już jest
    if dry_write:
        return True
    row = f"{slug},{voivodeship or ''},{population or ''},{country},{lat or ''},{lon or ''}\n"
    # Insert przed zagranicznymi (praha/berlin/vilnius) na końcu
    lines = existing.splitlines(keepends=True)
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        if line.startswith(("praha,", "berlin,", "vilnius,")):
            insert_idx = i
            break
    lines.insert(insert_idx, row)
    CITIES_META_CSV.write_text("".join(lines), encoding="utf-8")
    return True


def register_in_pipeline(slug: str, dry_write: bool = False) -> list[str]:
    """No-op: rejestracja miasta w pipeline jest teraz automatyczna.

    `run_pipeline.py` (discover_all_cities) i `scrape_all.sh` odkrywają miasta
    z `radoskop/cities/*/config.json` — samo utworzenie configu wystarcza, nie
    trzeba dopisywać sluga do żadnej listy. Wcześniej ta funkcja łatała dwie
    sztywne listy ALL_CITIES regexem; po przejściu na auto-discovery zostawiamy
    pustą implementację (zgodność API z callerami).
    """
    return []


# ---------------------------------------------------------------------------
# Per-slug orchestration
# ---------------------------------------------------------------------------

@dataclass
class CityReport:
    slug: str
    status: str  # OK | NO_ESESJA | ESESJA_EMPTY | NO_COMPOSITION | DRY_RUN_FAILED | SKIP
    city_name: str = ""
    voivodeship: str = ""
    population: Optional[int] = None
    councilors_found: int = 0
    sessions_in_kadencja: int = 0
    files_written: list[str] = field(default_factory=list)
    notes: str = ""


def process_slug(slug: str, *, register: bool = False, dry_write: bool = False,
                 skip_dry_run: bool = False) -> CityReport:
    """Pełen pipeline per miasto: probe → fetch composition → write → smoke test.

    Composition pobierana z PKW (oficjalne dane wyborów 2024-04-07). Fallback
    do eSesja landing jeśli slug nie jest w PKW (gminy <20k czasem mają inne
    formaty nazwy).
    """
    rep = CityReport(slug=slug, status="OK")

    # Skip jeśli config już istnieje (idempotentność). MUST być przed probe
    # żeby batch skipy były szybkie (bez sieciowego call do eSesja per slug).
    if (CITIES_DIR / slug / "config.json").is_file():
        rep.status = "SKIP"
        rep.notes = "config już istnieje, pomijam"
        return rep

    print(f"\n=== {slug} ===")
    print("  [1/5] probe eSesja...")
    probe = probe_esesja(slug)
    if not probe.works:
        rep.status = "NO_ESESJA"
        rep.notes = probe.error or "no esesja subdomain"
        print(f"    NO ESESJA: {rep.notes}")
        return rep
    print(f"    OK: {probe.title}")

    print("  [2/5] fetch PKW (dane rządowe wyborów 2024)...")
    comp = fetch_composition_pkw(slug)
    if not comp.councilors:
        print(f"    PKW miss: {comp.error}; fallback do eSesja landing")
        comp = fetch_composition_esesja(slug)
    if not comp.councilors:
        rep.status = "NO_COMPOSITION"
        rep.notes = comp.error or "brak składu — sprawdź ręcznie eSesja landing"
        print(f"    NO COMPOSITION: {rep.notes}")
        return rep
    rep.city_name = comp.city_name
    rep.voivodeship = comp.voivodeship
    rep.population = comp.population
    rep.councilors_found = len(comp.councilors)
    note_clubs = "z klubami" if any(c for c in comp.councilors.values()) else "BEZ klubów (NZ default)"
    print(f"    OK: {rep.city_name}, {rep.councilors_found} radnych "
          f"({note_clubs}, źródło: {comp.source})")

    print("  [3/5] generate config + scraper...")
    config = build_config(slug, comp, esesja_url=f"https://{slug}.esesja.pl")
    scraper_code = build_scraper_py(slug, rep.city_name, f"https://{slug}.esesja.pl")
    paths = write_city_files(slug, config, scraper_code, dry_write=dry_write)
    rep.files_written = [str(p) for p in paths]

    # cities-meta.csv — geocode via Photon, bo bez lat/lon mapa pomija pin
    coords = geocode_city(rep.city_name, country="pl") if not dry_write else None
    lat, lon = coords if coords else (None, None)
    if coords:
        print(f"    geocode: {lat:.4f}, {lon:.4f}")
    else:
        print(f"    geocode: brak — uzupełnij ręcznie w cities-meta.csv")
    added_meta = append_to_cities_meta(
        slug, comp.voivodeship, comp.population, lat, lon,
        dry_write=dry_write,
    )
    if added_meta:
        rep.files_written.append(str(CITIES_META_CSV))

    print("  [4/5] dry-run smoke test...")
    if dry_write or skip_dry_run:
        print("    skip")
    else:
        dr = run_dry_run(slug)
        if not dr.success:
            rep.status = "DRY_RUN_FAILED"
            rep.notes = dr.error
            print(f"    FAILED: {dr.error}")
            return rep
        rep.sessions_in_kadencja = dr.sessions_in_kadencja
        if dr.sessions_in_kadencja == 0:
            rep.status = "ESESJA_EMPTY"
            rep.notes = "eSesja istnieje ale brak sesji w 2024-2029"
            print(f"    EMPTY: 0 sesji w kadencji")
        else:
            print(f"    OK: {dr.sessions_in_kadencja} sesji w kadencji")

    print("  [5/5] register in pipeline...")
    if register:
        changed = register_in_pipeline(slug, dry_write=dry_write)
        rep.files_written.extend(changed)
        print(f"    OK: zaktualizowane {len(changed)} plik(ów) pipeline")
    else:
        print("    skip (--register nie podane)")

    return rep


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_batch(path: Path) -> list[str]:
    """Czyta plik wsadowy: jeden slug per linia, lub CSV ze slug w pierwszej kolumnie."""
    slugs: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                slug = line.split(",", 1)[0].strip()
            else:
                slug = line
            if slug and slug != "slug":  # pomijaj nagłówek CSV
                slugs.append(slug)
    return slugs


def run_probe_all_pkw(output_csv: Path, workers: int = 20,
                       cities_only: bool = True) -> int:
    """Mass probe eSesja dla wszystkich miast z PKW dataset.

    cities_only=True (default): tylko Rada Miasta + Rada Miejska (984 jedn.).
    Sortowanie po liczbie radnych malejąco (proxy dla wielkości).
    """
    import concurrent.futures
    data = load_pkw_data(cities_only=cities_only)
    slug_idx = _build_pkw_slug_index(data)
    items = sorted(slug_idx.items())
    print(f"Mass probe eSesja: {len(items)} gmin, {workers} workerów\n")

    rows: list[dict] = []
    done = 0
    start = time.time()

    def _one(args):
        slug, terc = args
        probe = probe_esesja(slug, timeout=10)
        radni = data[terc]
        return {
            "slug": slug,
            "terc": terc,
            "esesja_works": "yes" if probe.works else "no",
            "councilors": len(radni),
            "gmina": radni[0].get("gmina", ""),
            "wojewodztwo": radni[0].get("wojewodztwo_kod", ""),
            "error": probe.error if not probe.works else "",
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for row in ex.map(_one, items):
            rows.append(row)
            done += 1
            if done % 100 == 0:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(items) - done) / rate if rate > 0 else 0
                print(f"  {done}/{len(items)} ({rate:.1f}/s, ETA {eta:.0f}s)")

    # Sortuj: najpierw works=yes, potem councilors desc, potem slug
    rows.sort(key=lambda r: (
        0 if r["esesja_works"] == "yes" else 1,
        -r["councilors"],
        r["slug"],
    ))

    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "slug", "terc", "esesja_works", "councilors", "gmina",
            "wojewodztwo", "error",
        ])
        writer.writeheader()
        writer.writerows(rows)

    yes = sum(1 for r in rows if r["esesja_works"] == "yes")
    no_ = sum(1 for r in rows if r["esesja_works"] == "no")
    elapsed = time.time() - start
    print(f"\n=== Probe done in {elapsed:.0f}s ===")
    print(f"  eSesja: {yes} gmin")
    print(f"  Bez eSesja: {no_} gmin")
    print(f"  Output: {output_csv}")
    return 0


def print_summary(reports: list[CityReport]) -> None:
    print("\n" + "=" * 70)
    print("PODSUMOWANIE")
    print("=" * 70)
    by_status: dict[str, list[CityReport]] = {}
    for r in reports:
        by_status.setdefault(r.status, []).append(r)
    for status in ["OK", "ESESJA_EMPTY", "NO_ESESJA", "NO_COMPOSITION",
                   "DRY_RUN_FAILED", "SKIP"]:
        items = by_status.get(status, [])
        if not items:
            continue
        print(f"\n  {status} ({len(items)}):")
        for r in items:
            extra = []
            if r.councilors_found:
                extra.append(f"{r.councilors_found} radnych")
            if r.sessions_in_kadencja:
                extra.append(f"{r.sessions_in_kadencja} sesji")
            if r.notes:
                extra.append(r.notes)
            print(f"    - {r.slug}: {', '.join(extra) or '—'}")


def main() -> int:
    p = argparse.ArgumentParser(description="Masowe dodawanie miast do Radoskopu")
    p.add_argument("slugs", nargs="*", help="Slug(i) miasta do dodania")
    p.add_argument("--batch", type=Path, help="Plik z listą slugów (jeden per linia lub CSV)")
    p.add_argument("--probe-only", action="store_true",
                   help="Tylko sprawdź dostępność eSesja, nie generuj plików")
    p.add_argument("--register", action="store_true",
                   help="Dopisz do ALL_CITIES w scrape_all.sh + run_pipeline.py")
    p.add_argument("--no-write", action="store_true",
                   help="Nic nie zapisuj na dysku (dry-run dla samego skryptu)")
    p.add_argument("--list-pkw", action="store_true",
                   help="Wypisz wszystkie miasta z PKW dataset (slug → terc, "
                        "liczba radnych) i zakończ. Użyteczne do batch input.")
    p.add_argument("--probe-all-pkw", type=Path, default=None, metavar="CSV_PATH",
                   help="Probe eSesja dla wszystkich miast z PKW (984 miast: "
                        "Rada Miasta + Rada Miejska, bez gmin wiejskich). "
                        "Output CSV: slug,terc,esesja_works,councilors,gmina,error")
    p.add_argument("--include-villages", action="store_true",
                   help="Włącz też 'Rada Gminy' (gminy wiejskie). Default: tylko miasta")
    p.add_argument("--skip-dry-run", action="store_true",
                   help="Pomiń smoke test --dry-run scrapera (oszczędza 5-10s/miasto)")
    p.add_argument("--workers", type=int, default=20,
                   help="Liczba paralelnych workerów dla --probe-all-pkw (default 20)")
    args = p.parse_args()

    # Tryb listing: tylko wypisuje PKW dataset i kończy
    if args.list_pkw:
        data = load_pkw_data(cities_only=not args.include_villages)
        slug_idx = _build_pkw_slug_index(data)
        filter_note = "miast" if not args.include_villages else "wszystkich jednostek"
        print(f"PKW dataset ({filter_note}): {len(slug_idx)} slugów ({len(data)} terc)\n")
        for slug in sorted(slug_idx.keys()):
            terc = slug_idx[slug]
            radni = data[terc]
            gmina = radni[0].get("gmina", "")
            print(f"  {slug:35s}  {terc}  {len(radni):2d} radnych  {gmina}")
        return 0

    # Tryb mass probe: concurrent eSesja check wszystkich miast z PKW
    if args.probe_all_pkw:
        return run_probe_all_pkw(args.probe_all_pkw, workers=args.workers,
                                  cities_only=not args.include_villages)

    slugs: list[str] = list(args.slugs)
    if args.batch:
        slugs.extend(load_batch(args.batch))
    if not slugs:
        p.error("podaj co najmniej jeden slug albo --batch FILE")

    # Tryb probe-only
    if args.probe_only:
        print(f"Probe eSesja dla {len(slugs)} slugów:\n")
        for slug in slugs:
            probe = probe_esesja(slug)
            mark = "OK " if probe.works else "NIE"
            extra = probe.title if probe.works else probe.error
            print(f"  {mark}  {slug:25s}  {extra}")
            time.sleep(0.5)  # delicate rate limit
        return 0

    # Tryb pełny
    reports = []
    for i, slug in enumerate(slugs, 1):
        print(f"\n[{i}/{len(slugs)}]", end=" ")
        try:
            rep = process_slug(slug, register=args.register, dry_write=args.no_write,
                               skip_dry_run=args.skip_dry_run)
        except KeyboardInterrupt:
            print("\nPrzerwane przez użytkownika.")
            break
        except Exception as exc:
            rep = CityReport(slug=slug, status="DRY_RUN_FAILED",
                             notes=f"unexpected: {exc}")
            print(f"  EXCEPTION: {exc}")
        reports.append(rep)
        # delicate rate limit między miastami; ale jeśli skip, brak sieciowych
        # wywołań więc sleep zbędny
        if rep.status != "SKIP":
            time.sleep(0.3)

    print_summary(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
