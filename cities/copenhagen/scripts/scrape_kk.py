#!/usr/bin/env python3
"""Scraper Borgerrepræsentationen (København), kk.dk/dagsordener-og-referater.

USTALENIE PO RESEARCHU (2026-05-28, zweryfikowane na żywym serwisie):
Dania jest w Tier 4 z eu_council_voting_analysis.md (model identyczny z
Francją). W Borgerrepræsentationen głosowania protokołowane są pr. parti,
nie pr. medlem. Format Beslutning w referacie:

  "Indstillingen blev godkendt med 42 stemmer mod 4. 8 medlemmer undlod at
  stemme.
  For stemte: Ø (Charlotte Lund, Hassan Nur Wardere, ...), A, C, F, B, V, Å, I, O og Helle Jønch
  Imod stemte: Ø (Absalon Billehøj, ...)
  Undlod at stemme: Ø (Bente Møller, ...) og Troels Christian Jakobsen"

Zwykle bookstav reprezentuje całą partię (np. "A"). Przy partispalt
zapisuje się nazwiska w nawiasach po bookstavie. Løsgængere (Finn Rudaizky,
Helle Jønch, Troels Christian Jakobsen) wymieniani po imieniu, bez
bookstava. Większość punktów ma "Indstillingen blev godkendt uden
afstemning" (konsens) — wtedy vote_mode=show_of_hands.

CO ROBI TEN SCRAPER:
  --scrape   pełny scrape: paginacja indeksu BR (~24 strony, 598 posiedzeń),
             pobranie każdego møde-{DDMMYYYY}/referat, odkrycie punktów,
             parsowanie sekcji Beslutning, zapis docs/kadencja-{id}.json.
  --m URL    jedno posiedzenie (wszystkie punkty), zapis kadencja json.
  --punkt URL  jeden punkt (debug), wypisuje sparsowany rekord.

Kontrakt danych: radoskop-premium/strategia/GLOSOWANIA_FRAKCYJNE.md
Klasyfikacja krajowa: radoskop-premium/strategia/eu_council_voting_analysis.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

CITY_DIR = Path(__file__).resolve().parents[1]          # cities/copenhagen
REPO_DIR = CITY_DIR.parents[1]                          # radoskop
sys.path.insert(0, str(REPO_DIR / "scripts"))

from lib_faction_votes import make_faction_vote  # noqa: E402

BASE = "https://www.kk.dk"
INDEX_PATH = "/dagsordener-og-referater/Borgerrepr%C3%A6sentationen"
INDEX_URL = BASE + INDEX_PATH
ROSTER_URL = BASE + "/politik/borgerrepraesentationen/medlemsoversigt"
UA = "radoskop-copenhagen/1.0 (+https://radoskop.eu)"

# Pełne nazwy partii (etykieta na medlemsoversigt) -> kod klubu z config.clubs.
PARTY_LABEL_TO_CLUB: dict[str, str] = {
    "Enhedslisten": "OE",
    "Socialdemokratiet": "A",
    "Det Konservative Folkeparti": "C",
    "Radikale Venstre": "B",
    "Socialistisk Folkeparti": "F",
    "Venstre": "V",
    "Liberal Alliance": "I",
    "Alternativet": "AA",
    "Dansk Folkeparti": "O",
    "Frie Grønne": "Q",
    "Frie Gronne": "Q",  # fallback bez znaku ø
}

# Bookstaver partii widziane w referatach BR. Mapowanie na kody clubs z
# config.json (letter_to_club). Bookstaver "Q" (Frie Grønne) i "Å"
# (Alternativet) używają znaków UTF-8 — dlatego regex bookstavów obsługuje
# zarówno ASCII jak i nadkomplet polish-friendly.
LETTER_TO_CLUB: dict[str, str] = {
    "Ø": "OE", "A": "A", "C": "C", "F": "F", "B": "B",
    "V": "V", "Å": "AA", "I": "I", "O": "O", "Q": "Q",
}

# Niezrzeszeni (Løsgængere) wpadają jako klub NZ. Lista znana — Finn
# Rudaizky, Helle Jønch, Troels Christian Jakobsen w kadencji 2022-2025.
# W kadencji 2026-2029 sprawdzimy przy pierwszym scrape, dlatego klucz
# jest poszerzalny.
NZ_CLUB = "NZ"


def fetch_text(url: str, timeout: int = 30) -> str:
    """Pobierz HTML strony. requests jako lazy import."""
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def html_to_text(html: str) -> str:
    """Strip HTML tagów i sklejenie whitespace. Dla treści referatów
    wystarczy proste regexowe odchudzenie — strony są Drupal 11 statyczne,
    bez JS-rendered contentu."""
    import html as _html
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Indeks posiedzeń ─────────────────────────────────────────────────
MEETING_LINK_RE = re.compile(
    r"/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-(\d{8})/referat\b",
    re.IGNORECASE,
)


# ── Roster (lista radnych) ────────────────────────────────────────────
PARTY_LABEL_RE_TEXT = "|".join(re.escape(k) for k in PARTY_LABEL_TO_CLUB.keys())
# Medlemsoversigt renderuje każdy wpis jako: "<parti> | Borgerrepræsentationen ... <a href='/politik/borgerrepraesentationen/medlemsoversigt/{slug}' lub '/politik/borgmestre/{slug}'>NAZWISKO</a>" + opis ról ("Medlem af X og Y", "X-borgmesteren er forperson for Y").
# Łapiemy też tekst od końca nazwiska do początku następnego wpisu, żeby
# wyciągnąć udvalg (komisje).
ROSTER_ENTRY_RE = re.compile(
    rf"(?:(?P<p1>{PARTY_LABEL_RE_TEXT})\s*\|\s*Borgerrepr[æae]sentationen|"
    rf"Borgerrepr[æae]sentationen\s*\|\s*(?P<p2>{PARTY_LABEL_RE_TEXT}))"
    r".*?<a[^>]+href=[\"']"
    r"(?P<href>/politik/(?:borgerrepraesentationen/medlemsoversigt|borgmestre)/[^\"']+)"
    r"[\"'][^>]*>(?P<name>[^<]+)</a>"
    r"(?P<after>.*?)"
    rf"(?=(?:{PARTY_LABEL_RE_TEXT})\s*\|\s*Borgerrepr[æae]sentationen|"
    rf"Borgerrepr[æae]sentationen\s*\|\s*(?:{PARTY_LABEL_RE_TEXT})|\Z)",
    re.IGNORECASE | re.DOTALL,
)
SUPPLEANT_TAG_RE = re.compile(r"\s*\(suppleant\)\s*$", re.IGNORECASE)
ORLOV_TAG_RE = re.compile(r"\s*\(orlov\)\s*$", re.IGNORECASE)

# "Medlem af Økonomiudvalget" / "Medlem af A og B" / "Midlertidigt medlem af X og Y med første dag den Z og indtil videre."
MEDLEM_AF_RE = re.compile(
    r"(?:Midlertidigt\s+)?[Mm]edlem\s+af\s+(?P<udvalg>[^.]+?)(?:\s+med f[øo]rste dag\b|\s+og indtil\b|\.|$)",
    re.IGNORECASE,
)
# Borgmistrz: "Beskæftigelses-, integrations- og erhvervsborgmesteren er forperson for Beskæftigelses-, Integrations- og Erhvervsudvalget."
BORGMESTER_FORPERSON_RE = re.compile(
    r"borgmester(?:en)?\s+er\s+forperson\s+for\s+(?P<udvalg>[^.]+?)(?:\.|$)",
    re.IGNORECASE,
)

# Normalizacja nazwy udvalg: spróbuj sprowadzić do kanonicznej wersji z
# config.json. Strip whitespace, kropki, "og". Match po słowach kluczowych.
CANONICAL_UDVALG: dict[str, str] = {
    "okonomi": "Økonomiudvalget",
    "social": "Socialudvalget",
    "born": "Børne- og Ungdomsudvalget",
    "klima": "Klima-, Miljø- og Teknikudvalget",
    "kultur": "Kultur-, Fritids- og Borgerserviceudvalget",
    "beskaeft": "Beskæftigelses-, Integrations- og Erhvervsudvalget",
    "sundhed": "Sundheds- og Omsorgsudvalget",
}


def _normalize_udvalg(raw: str) -> str | None:
    """Zwróć kanoniczną nazwę udvalg albo None gdy nie rozpoznane."""
    if not raw:
        return None
    # Strip akcentów do ASCII dla matchowania
    raw = raw.strip().rstrip(".").strip()
    ascii_lc = (raw.lower()
                .replace("ø", "o").replace("å", "a").replace("æ", "ae")
                .replace("ö", "o").replace("ä", "a"))
    for needle, canonical in CANONICAL_UDVALG.items():
        if needle in ascii_lc:
            return canonical
    return raw  # zostaw oryginał gdy nie pasuje do żadnego znanego udvalg


def _udvalg_from_borgmester_title(title: str) -> str | None:
    """Mapuj tytuł borgmistrzowski na udvalg.

    Tytuły z kk.dk: 'overborgmester', 'socialborgmester',
    'sundheds- og omsorgsborgmester', 'klima-, miljø- og teknikborgmester',
    'kultur-, fritids- og borgerserviceborgmester',
    'børne- og ungdomsborgmester',
    'beskæftigelses-, integrations- og erhvervsborgmester'.
    """
    if not title:
        return None
    t = (title.lower()
         .replace("ø", "o").replace("å", "a").replace("æ", "ae"))
    if "overborgmester" in t:
        return "Økonomiudvalget"
    if "social" in t:
        return "Socialudvalget"
    if "born" in t or "ungdom" in t:
        return "Børne- og Ungdomsudvalget"
    if "klima" in t or "miljo" in t or "teknik" in t:
        return "Klima-, Miljø- og Teknikudvalget"
    if "kultur" in t or "fritid" in t or "borgerservice" in t:
        return "Kultur-, Fritids- og Borgerserviceudvalget"
    if "beskaeft" in t or "integration" in t or "erhverv" in t:
        return "Beskæftigelses-, Integrations- og Erhvervsudvalget"
    if "sundhed" in t or "omsorg" in t:
        return "Sundheds- og Omsorgsudvalget"
    return None


def _parse_udvalg(text: str) -> list[str]:
    """Wyciągnij listę udvalg z tekstu po nazwisku radnego.

    Zwraca listę kanonicznych nazw, deduplikowaną, w kolejności pojawienia.
    Obsługuje 'Medlem af X og Y' oraz 'X-borgmesteren er forperson for Y'.

    Krytyczny detail: nazwy udvalgów same zawierają " og " ("Sundheds- og
    Omsorgsudvalget", "Børne- og Ungdomsudvalget", "Beskæftigelses-,
    Integrations- og Erhvervsudvalget"). Nie można naiwnie splittować po
    " og ". Zamiast tego skanujemy text na znane kanoniczne nazwy
    (substring match, akcent-insensitive).
    """
    found: list[str] = []
    seen: set[str] = set()

    def _ascii_lc(s: str) -> str:
        return (s.lower()
                .replace("ø", "o").replace("å", "a").replace("æ", "ae"))

    def _add(canonical: str, suffix: str = "") -> None:
        label = f"{canonical}{suffix}"
        if label in seen:
            return
        seen.add(label)
        found.append(label)

    # Lista kanonicznych BR udvalgów (7 stałych). Lokaludvalg nie scanu-
    # jemy bo to inne ciało (dzielnicowe, nie BR).
    BR_UDVALG = [
        "Økonomiudvalget",
        "Socialudvalget",
        "Børne- og Ungdomsudvalget",
        "Klima-, Miljø- og Teknikudvalget",
        "Kultur-, Fritids- og Borgerserviceudvalget",
        "Beskæftigelses-, Integrations- og Erhvervsudvalget",
        "Sundheds- og Omsorgsudvalget",
    ]

    # Borgmistrz jako forperson — wykryj NAJPIERW, żeby nie nadpisać
    # plain medlem-of dla tej samej osoby/udvalgu.
    forperson_udvalg: set[str] = set()
    text_ascii = _ascii_lc(text)
    for m in BORGMESTER_FORPERSON_RE.finditer(text):
        raw_ascii = _ascii_lc(m.group("udvalg"))
        for canon in BR_UDVALG:
            if _ascii_lc(canon) in raw_ascii:
                forperson_udvalg.add(canon)
                _add(canon, " (forperson)")

    # "Medlem af" — bierzemy cały segment od "af" do końca zdania, potem
    # po prostu sprawdzamy, które kanoniczne udvalgi są w nim obecne.
    for m in MEDLEM_AF_RE.finditer(text):
        seg_ascii = _ascii_lc(m.group("udvalg"))
        for canon in BR_UDVALG:
            if canon in forperson_udvalg:
                continue  # już dodane jako forperson
            if _ascii_lc(canon) in seg_ascii:
                _add(canon)

    return found


def fetch_roster(max_pages: int = 5) -> list[dict]:
    """Pobierz medlemsoversigt BR i zwróć listę radnych z partią i udvalgami.

    Format wpisu: {name, slug, club, is_suppleant, profile_url, komisje}.
    Slug pochodzi z URL kk.dk (ostatni segment). Duplikaty (po slugu) usuwane.
    """
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for page in range(max_pages):
        url = f"{ROSTER_URL}?page={page}"
        try:
            html = fetch_text(url)
        except Exception as e:
            print(f"  roster page={page} padł: {e}", file=sys.stderr)
            break
        before = len(out)
        for m in ROSTER_ENTRY_RE.finditer(html):
            party_label = m.group("p1") or m.group("p2")
            club = PARTY_LABEL_TO_CLUB.get(party_label)
            href = m.group("href")
            name_raw = m.group("name").strip()
            is_supp = bool(SUPPLEANT_TAG_RE.search(name_raw))
            is_orlov = bool(ORLOV_TAG_RE.search(name_raw))
            name = SUPPLEANT_TAG_RE.sub("", name_raw)
            name = ORLOV_TAG_RE.sub("", name).strip()
            # Borgmistrz: "Andreas Keil, beskæftigelses-, integrations- og erhvervsborgmester"
            # — wyciągamy tytuł (po przecinku) jako fallback dla forperson
            # gdy główny opis "X-borgmesteren er forperson for Y" jest pusty.
            borgmester_title = ""
            if "," in name and "borgmester" in name.lower():
                parts = name.split(",", 1)
                name = parts[0].strip()
                borgmester_title = parts[1].strip()
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            # Tekst po linku do następnego wpisu zawiera "Medlem af X og Y".
            after_text = html_to_text(m.group("after") or "")
            komisje = _parse_udvalg(after_text)
            # Fallback: jeśli borgmistrz nie ma forperson w 'after' ale ma tytuł
            # w nazwisku (np. Line Barfod, klima-...), zmapuj tytuł na udvalg.
            if borgmester_title and not any("forperson" in k for k in komisje):
                fallback = _udvalg_from_borgmester_title(borgmester_title)
                if fallback:
                    komisje = [f"{fallback} (forperson)"] + [k for k in komisje if k != fallback]
            out.append({
                "name": name,
                "slug": slug,
                "club": club,
                "is_suppleant": is_supp,
                "is_orlov": is_orlov,
                "profile_url": BASE + href,
                "komisje": komisje,
            })
        if len(out) == before:
            break  # pusta strona, koniec
    return out


def discover_meeting_urls(max_pages: int = 30) -> list[tuple[str, str]]:
    """Zwraca [(url_referatu, data_DDMMYYYY)] dla wszystkich BR posiedzeń.

    Indeks ma ~24 strony (paginacja ?page=0..23). Stronka pokazuje 25
    wyników na stronę, ale i tak iterujemy aż do pierwszej pustej.
    Najnowsze posiedzenia idą jako pierwsze.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for page in range(max_pages):
        url = f"{INDEX_URL}?page={page}"
        try:
            html = fetch_text(url)
        except Exception as e:
            print(f"  indeks page={page} padł: {e}", file=sys.stderr)
            break
        before = len(out)
        for m in MEETING_LINK_RE.finditer(html):
            full = BASE + m.group(0)
            if full in seen:
                continue
            seen.add(full)
            out.append((full, m.group(1)))
        if len(out) == before:
            break  # pusta strona, koniec
    return out


# ── Lista punktów w jednym posiedzeniu ───────────────────────────────
PUNKT_LINK_RE = re.compile(
    r"/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-(\d{8})/referat/punkt-(\d+)\b",
    re.IGNORECASE,
)


def discover_punkt_urls(meeting_html: str) -> list[tuple[str, int]]:
    """[(url, punkt_numer)] dla wszystkich punktów posiedzenia w kolejności."""
    seen: dict[int, str] = {}
    for m in PUNKT_LINK_RE.finditer(meeting_html):
        n = int(m.group(2))
        if n not in seen:
            seen[n] = BASE + m.group(0)
    return [(seen[n], n) for n in sorted(seen)]


# ── Parsowanie sekcji Beslutning ─────────────────────────────────────
BESLUTNING_RE = re.compile(
    r"Beslutning\s+Borgerrepr[æae]sentationens beslutning"
    r".*?",
    re.IGNORECASE,
)

# Wyniki: "vedtaget med X stemmer imod Y", "godkendt med X stemmer mod Y",
# "forkastet med X stemmer for Y", "blev forkastet med Y stemmer mod X".
OUTCOME_RE = re.compile(
    r"(?P<outcome>vedtaget|godkendt|tiltr[ae]dt|forkastet)\s+med\s+"
    r"(?P<n1>\d+)\s+stemmer?\s+(?P<dir>imod|mod|for)\s+(?P<n2>\d+)",
    re.IGNORECASE,
)
ABSTAIN_RE = re.compile(
    r"(?P<n>\d+)\s+medlemmer?\s+undlod\s+at\s+stemme",
    re.IGNORECASE,
)
ZERO_ABSTAIN_RE = re.compile(
    r"Ingen\s+medlemmer\s+undlod\s+at\s+stemme",
    re.IGNORECASE,
)
UDEN_AFSTEMNING_PASSIVE_RE = re.compile(
    # Pasywny: "<noun> blev <outcome> ... uden afstemning | enstemmigt".
    # Outcome to imiesłów: godkendt / vedtaget / tiltrådt / forkastet /
    # trukket tilbage / udsat.
    r"(?:Indstillingen|Medlemsforslaget|Foresp[øo]rgslen|Forslaget|"
    r"Ændringsforslaget|Bekendtg[øo]relsen|Sagen|Punktet|Det)\s+blev\s+"
    r"(?P<outcome>godkendt|vedtaget|tiltr[ae]dt|forkastet|trukket\s+tilbage|udsat)"
    r"[^.]*?"
    r"(?:uden\s+afstemning|ved\s+enstemmighed|enstemmigt)",
    re.IGNORECASE,
)
UDEN_AFSTEMNING_ACTIVE_RE = re.compile(
    # Aktywny: "Borgerrepræsentationen <verb> ... uden afstemning". Outcome to
    # czasownik w czasie przeszłym: vedtog / godkendte / tiltrådte / udsatte /
    # forkastede.
    r"Borgerrepr[æae]sentationen\s+"
    r"(?P<outcome>vedtog|godkendte|tiltr[åa]dte|udsatte|forkastede)"
    r"[^.]*?"
    r"(?:uden\s+afstemning|ved\s+enstemmighed|enstemmigt)",
    re.IGNORECASE,
)
# Wycofane/odroczone (trukket tilbage, udsat, udsatte) NIE są odrzuceniem
# — to osobny status. passed=None oznacza "rezolucja nie zapadła w tym
# punkcie", zamiast mylącego False.
NEUTRAL_OUTCOMES = ("trukket", "udsat", "udsatte")
POSITIVE_OUTCOMES = ("godkendt", "vedtaget", "tiltr", "vedtog", "godkendte", "tiltr")
NEGATIVE_OUTCOMES = ("forkastet", "forkastede")
# Wycofane/odroczone (trukket tilbage, udsat) NIE są odrzuceniem — to
# osobny status. passed=None oznacza "rezolucja nie zapadła w tym punkcie",
# zamiast mylącego False.
NEUTRAL_OUTCOMES = ("trukket", "udsat", "udsatte")

# Linie z bookstavami i nazwiskami. Format:
#   "For stemte: Ø (Charlotte Lund, ...), A, C, F, B, V, Å, I, O og Finn Rudaizky (Løsgænger)"
# Linia kończy się przed kolejnym nagłówkiem ("Imod stemte:", "Undlod
# at stemme:", "Bilag", "Til top", koniec sekcji) albo nową sekcją.
SECTION_FOR_RE = re.compile(
    r"For\s+stemte\s*:\s*(?P<body>.+?)(?=\s+(?:Imod\s+stemte|Undlod\s+at\s+stemme|Hverken\s+for|Bilag|Til top|$))",
    re.IGNORECASE | re.S,
)
SECTION_AGAINST_RE = re.compile(
    r"Imod\s+stemte\s*:\s*(?P<body>.+?)(?=\s+(?:Undlod\s+at\s+stemme|Hverken\s+for|For\s+stemte|Bilag|Til top|$))",
    re.IGNORECASE | re.S,
)
SECTION_ABSTAIN_RE = re.compile(
    r"Undlod\s+at\s+stemme\s*:\s*(?P<body>.+?)(?=\s+(?:For\s+stemte|Imod\s+stemte|Hverken\s+for|Bilag|Til top|$))",
    re.IGNORECASE | re.S,
)

# Bookstav partii: pojedynczy duży znak, opcjonalnie z natychmiastowym
# nawiasem nazwisk dla rozłamu wewnątrz partii.
#   "Ø", "Ø (Charlotte Lund, Hassan...)" -> bookstav=Ø, names=...
LETTERS = "".join(LETTER_TO_CLUB.keys())  # ØACFBVÅIOQ
TOKEN_LETTER_RE = re.compile(
    rf"(?P<letter>[{LETTERS}])(?:\s*\((?P<names>[^)]+)\))?",
)
# Nazwisko niezrzeszonego: ciąg słów z opcjonalnym (Løsgænger) na końcu.
# Łapiemy gdy poza nawiasem po bookstavie i poza listą bookstavów.
TOKEN_NAME_RE = re.compile(
    r"(?P<name>[A-ZÆØÅ][A-Za-zæøåÆØÅ.\-'’ ]{2,}?)"
    r"(?:\s*\((?:Løsgænger|UFP|partil[øo]s)[^)]*\))?",
)


def _split_section_body(body: str) -> tuple[dict[str, list[str]], list[str]]:
    """Rozbij listę głosujących na (per_letter_names, independent_names).

    body: "Ø (Charlotte Lund, ...), A, C, F, B, V, Å, I, O og Finn Rudaizky (Løsgænger)"

    Zwraca:
        per_letter_names: {bookstav: [nazwiska...]} — pusta lista jeśli
            partia głosowała w bloku bez rozłamu (cały bookstav).
        independent_names: ["Finn Rudaizky", ...] — Løsgængere wymienieni
            po nazwisku.
    """
    per_letter: dict[str, list[str]] = {}
    independents: list[str] = []
    # Iteruj po body, naprzemiennie wyciągając bookstav-z-nawiasem albo
    # nazwisko. Strategia: szukaj wszystkich bookstav-tokenów, każdy ich
    # match wykreśl z body, reszta to imiona/nazwiska niezrzeszonych.
    rest = body
    for m in TOKEN_LETTER_RE.finditer(body):
        # Bookstav musi być wolno stojącym tokenem: poprzedzony nie-literą
        # (start linii / przecinek / spacja) i, gdy brak nawiasu nazwisk,
        # następujący po nim też nie-litera (żeby "F" w "Finn" lub "C" w
        # "Christian" nie wpadły jako bookstav).
        start = m.start()
        if start > 0 and re.match(r"[A-Za-zæøåÆØÅ]", body[start - 1]):
            continue
        names_raw = (m.group("names") or "").strip()
        if not names_raw:
            end = m.end()
            if end < len(body) and re.match(r"[A-Za-zæøåÆØÅ]", body[end]):
                continue
        letter = m.group("letter")
        names = [n.strip() for n in re.split(r",|\s+og\s+", names_raw) if n.strip()] if names_raw else []
        per_letter.setdefault(letter, []).extend(names)
        rest = rest.replace(m.group(0), " ", 1)
    # Po wykreśleniu bookstavów reszta to oddzielacze i nazwiska niezrzeszonych.
    rest = re.sub(r"\s+og\s+", ", ", rest, flags=re.IGNORECASE)
    rest = rest.replace(",", " , ")
    for chunk in re.split(r"\s*,\s*", rest):
        chunk = chunk.strip(" .")
        if not chunk:
            continue
        # Wytnij "(Løsgænger)" / "(UFP)" / "(partiløs)".
        chunk = re.sub(r"\((?:Løsgænger|UFP|partil[øo]s)[^)]*\)", "", chunk, flags=re.IGNORECASE).strip()
        # Token musi wyglądać jak imię + nazwisko (min. 2 słowa, kapitalik).
        if re.match(r"[A-ZÆØÅ][A-Za-zæøåÆØÅ.\-'’]+(?:\s+[A-ZÆØÅ][A-Za-zæøåÆØÅ.\-'’]+)+", chunk):
            independents.append(chunk)
    return per_letter, independents


def _seats_lookup(config: dict) -> dict[str, int]:
    """Mapa kod_klubu -> seats z config.clubs."""
    return {code: int(meta.get("seats") or 0) for code, meta in (config.get("clubs") or {}).items()}


def _parse_beslutning_block(text: str, config: dict) -> dict | None:
    """Wyjmij wynik głosowania z bloku Beslutning. Zwraca None, jeśli brak
    głosowania (sekcji nie ma w punkcie)."""
    if not re.search(r"Beslutning\b", text):
        return None
    # Znajdź początek sekcji Beslutning (do końca / do Bilag / Til top).
    m_start = re.search(r"Beslutning\b", text)
    if not m_start:
        return None
    block = text[m_start.start():]
    block = re.split(r"\b(?:Bilag\b|Til top)", block, maxsplit=1)[0]

    # Variant A: uden afstemning / enstemmighed (passive or active form).
    m_uden = UDEN_AFSTEMNING_PASSIVE_RE.search(block) or UDEN_AFSTEMNING_ACTIVE_RE.search(block)
    if m_uden:
        outcome_raw = m_uden.group("outcome").lower()
        # Pozytywne: godkendt/vedtaget/tiltrådt (i czasowe vedtog/godkendte).
        # Neutralne (trukket tilbage / udsat): passed=None — nie odrzucenie,
        # tylko brak rozstrzygnięcia w danym punkcie.
        # Negatywne: forkastet / forkastede.
        if outcome_raw.startswith(NEUTRAL_OUTCOMES):
            passed = None
        elif outcome_raw.startswith(NEGATIVE_OUTCOMES):
            passed = False
        elif outcome_raw.startswith(POSITIVE_OUTCOMES):
            passed = True
        else:
            passed = None  # nieznany outcome — neutralny zamiast zgadywać
        return {
            "mode": "show_of_hands",
            "passed": passed,
            "result": outcome_raw,
            "modalite": "uden_afstemning",
            "counts": {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0},
            "faction_votes": {},
            "named_votes": {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []},
        }

    # Variant B: formalne głosowanie z liczbami i listami.
    m_out = OUTCOME_RE.search(block)
    if not m_out:
        return None
    n1 = int(m_out.group("n1"))
    n2 = int(m_out.group("n2"))
    direction = m_out.group("dir").lower()
    outcome_raw = m_out.group("outcome").lower()
    passed = outcome_raw.startswith(("godkendt", "vedtaget", "tiltr"))
    # "vedtaget med 43 stemmer imod 7" -> n1=za, n2=przeciw
    # "forkastet med 43 stemmer for 7" -> n1=przeciw, n2=za
    if direction in ("imod", "mod"):
        za, przeciw = n1, n2
    else:  # "for" — to forkastet z odwróconym układem
        za, przeciw = n2, n1

    wstrz = 0
    if ZERO_ABSTAIN_RE.search(block):
        wstrz = 0
    else:
        m_ab = ABSTAIN_RE.search(block)
        if m_ab:
            wstrz = int(m_ab.group("n"))

    # Sekcje For stemte / Imod stemte / Undlod.
    for_body = SECTION_FOR_RE.search(block)
    against_body = SECTION_AGAINST_RE.search(block)
    abstain_body = SECTION_ABSTAIN_RE.search(block)

    seats = _seats_lookup(config)
    letter_to_club = config.get("letter_to_club") or LETTER_TO_CLUB

    # Per-klub: zlicz głosy. Każdy bookstav reprezentuje albo całą partię
    # (bez nawiasu) albo podzbiór (z nawiasem nazwisk). Bez nawiasu
    # przyjmujemy że WSZYSCY radni partii głosowali w tym kierunku;
    # z nawiasem — tylko ci wymienieni.
    faction_tallies: dict[str, dict[str, int]] = {}
    named_per_cat: dict[str, list[str]] = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}

    def _attribute(per_letter: dict[str, list[str]], independents: list[str], category: str) -> None:
        # Partie (bookstav)
        for letter, names in per_letter.items():
            club = letter_to_club.get(letter)
            if not club:
                continue
            tally = faction_tallies.setdefault(club, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0})
            if names:
                # Partispalt — tylko wymienieni członkowie
                tally[category] += len(names)
                named_per_cat[category].extend(f"{n} ({letter})" for n in names)
            else:
                # Cały blok partii głosował w tym kierunku
                tally[category] += seats.get(club, 0)
        # Niezrzeszeni (Løsgængere) -> klub NZ
        if independents:
            tally = faction_tallies.setdefault(NZ_CLUB, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0})
            tally[category] += len(independents)
            named_per_cat[category].extend(independents)

    if for_body:
        per_l, inds = _split_section_body(for_body.group("body"))
        _attribute(per_l, inds, "za")
    if against_body:
        per_l, inds = _split_section_body(against_body.group("body"))
        _attribute(per_l, inds, "przeciw")
    if abstain_body:
        per_l, inds = _split_section_body(abstain_body.group("body"))
        _attribute(per_l, inds, "wstrzymal_sie")

    # nieobecni = total - (za + przeciw + wstrz)
    total = sum(int(meta.get("seats") or 0) for meta in (config.get("clubs") or {}).values())
    if total <= 0:
        total = int(config.get("councilor_count") or 0)
    sum_voted = sum(sum(t.values()) for t in faction_tallies.values())
    nieob = max(0, total - sum_voted) if total else 0
    if nieob and faction_tallies:
        # Rozdziel nieobecnych po partiach proporcjonalnie? Nie — wpadają
        # jako kategoria zbiorcza w counts, nie per klub (bez danych).
        pass

    counts = {
        "za": za,
        "przeciw": przeciw,
        "wstrzymal_sie": wstrz,
        "brak_glosu": 0,
        "nieobecni": nieob,
    }

    return {
        "mode": "faction",
        "passed": passed,
        "result": outcome_raw,
        "modalite": "navnlig" if for_body or against_body else "samlet",
        "counts": counts,
        "faction_votes": faction_tallies,
        "named_votes": named_per_cat,
    }


# ── Tytuł punktu ─────────────────────────────────────────────────────
H1_RE = re.compile(r"<h1[^>]*>(?P<title>.+?)</h1>", re.IGNORECASE | re.S)


def _extract_topic(html: str) -> str:
    """Tytuł punktu z <h1>. Fallback: <title> bez sufiksu serwisu."""
    m = H1_RE.search(html)
    if m:
        title = html_to_text(m.group("title"))
        # Filtruj nagłówki niepunktowe (np. "Du er her").
        if title and not title.lower().startswith(("du er her", "primær")):
            return title
    m = re.search(r"<title>(.+?)</title>", html, re.IGNORECASE | re.S)
    if m:
        title = html_to_text(m.group(1))
        # "Tytuł | Københavns Kommune" -> "Tytuł"
        return re.sub(r"\s*\|\s*Københavns Kommune\s*$", "", title).strip()
    return ""


# ── Parsowanie jednego punktu ────────────────────────────────────────
def parse_punkt(url: str, html: str, config: dict, *, meeting_date_iso: str | None = None) -> dict | None:
    """Sparsuj stronę punktu -> rekord vote albo None gdy brak Beslutning."""
    topic = _extract_topic(html) or f"Punkt {url.rsplit('-', 1)[-1]}"
    text = html_to_text(html)
    decision = _parse_beslutning_block(text, config)
    if decision is None:
        return None

    # Numer punktu z URL.
    m_pn = re.search(r"/punkt-(\d+)$", url)
    punkt_n = int(m_pn.group(1)) if m_pn else None
    # Data posiedzenia z URL referatu jeśli nie podana.
    if not meeting_date_iso:
        m_dd = re.search(r"/m%C3%B8de-(\d{2})(\d{2})(\d{4})/", url)
        if m_dd:
            dd, mm, yyyy = m_dd.group(1), m_dd.group(2), m_dd.group(3)
            meeting_date_iso = f"{yyyy}-{mm}-{dd}"

    vote_id = f"copenhagen_{meeting_date_iso or '0000-00-00'}_punkt-{punkt_n or 0}"
    # kk.dk nie numeruje sesji BR sekwencyjnie — używamy daty jako session_number
    # (DD.MM.YYYY zgodnie z duńską konwencją). Dzięki temu groupBy po
    # session_number w template'cie nie tworzy 'Møde null'.
    session_number = None
    if meeting_date_iso and len(meeting_date_iso) == 10:
        session_number = f"{meeting_date_iso[8:10]}.{meeting_date_iso[5:7]}.{meeting_date_iso[0:4]}"

    if decision["mode"] == "show_of_hands":
        # Brak counts, brak faction_votes. Rekord vote z vote_mode show_of_hands.
        return {
            "id": vote_id,
            "session_date": meeting_date_iso,
            "session_number": session_number,
            "source_url": url,
            "topic": topic,
            "punkt": punkt_n,
            "result": decision["result"],
            "passed": decision["passed"],
            "modalite": decision["modalite"],
            "vote_mode": "show_of_hands",
            "counts": decision["counts"],
            "named_votes": decision["named_votes"],
            "faction_votes": {},
        }

    # Faction-mode — buduj rekord przez lib_faction_votes.make_faction_vote.
    seats = _seats_lookup(config)
    vote = make_faction_vote(
        vote_id=vote_id,
        session_date=meeting_date_iso or "",
        topic=topic,
        faction_tallies=decision["faction_votes"],
        club_seats=seats,
        session_number=session_number,
        source_url=url,
        result=decision["result"],
    )
    # Dołóż counts z tekstu (dokładniejsze niż suma z faction_votes gdy
    # są nieobecni / partispalt).
    vote["counts"] = decision["counts"]
    # Dołóż named_votes — przy partispalt mamy nazwiska osób z rozłamem.
    if any(decision["named_votes"].get(c) for c in decision["named_votes"]):
        vote["named_votes"] = decision["named_votes"]
    vote["punkt"] = punkt_n
    vote["passed"] = decision["passed"]
    vote["modalite"] = decision["modalite"]
    return vote


# ── Scrape orchestration ─────────────────────────────────────────────
def _ddmmyyyy_to_iso(s: str) -> str:
    if len(s) != 8 or not s.isdigit():
        return ""
    return f"{s[4:8]}-{s[2:4]}-{s[0:2]}"


def _kadencja_for_date(iso: str, config: dict) -> str:
    """Mapuj datę posiedzenia na klucz kadencji z config['kadencje']."""
    if not iso:
        return config.get("kadencja_active", "")
    kadencje = config.get("kadencje") or {}
    for kid, meta in kadencje.items():
        start = meta.get("start") or ""
        end = meta.get("end") or "9999-12-31"
        if start <= iso <= end:
            return kid
    return config.get("kadencja_active", "")


def scrape_meeting(meeting_url: str, config: dict) -> tuple[list[dict], str]:
    """Pobierz jedno posiedzenie + wszystkie jego punkty. Zwraca (votes, iso_date)."""
    html = fetch_text(meeting_url)
    # Data z URL referatu.
    m_dd = re.search(r"m%C3%B8de-(\d{8})/referat\b", meeting_url)
    iso = _ddmmyyyy_to_iso(m_dd.group(1)) if m_dd else ""
    punkts = discover_punkt_urls(html)
    votes: list[dict] = []
    for purl, n in punkts:
        try:
            phtml = fetch_text(purl)
            v = parse_punkt(purl, phtml, config, meeting_date_iso=iso)
            if v is not None:
                votes.append(v)
        except Exception as e:
            print(f"    punkt-{n} {purl}: {e}", file=sys.stderr)
    return votes, iso


def scrape(out_dir: Path, config: dict, limit_meetings: int | None = None) -> Path:
    """Pełny scrape: indeks -> posiedzenia -> punkty -> kadencja-{id}.json.

    Grupuje rekordy po kadencji (config.kadencje). Pisze osobny plik dla
    każdej kadencji + data.json z manifestem.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    meetings = discover_meeting_urls()
    if limit_meetings:
        meetings = meetings[:limit_meetings]

    print(f"[copenhagen] odkryto {len(meetings)} posiedzeń BR", file=sys.stderr)

    by_kadencja: dict[str, list[dict]] = {}
    sessions_per_kad: dict[str, set[str]] = {}
    for url, ddmmyyyy in meetings:
        iso = _ddmmyyyy_to_iso(ddmmyyyy)
        kid = _kadencja_for_date(iso, config)
        try:
            votes, _ = scrape_meeting(url, config)
        except Exception as e:
            print(f"  POMINIĘTO {url}: {e}", file=sys.stderr)
            continue
        by_kadencja.setdefault(kid, []).extend(votes)
        if iso:
            sessions_per_kad.setdefault(kid, set()).add(iso)
        print(f"  {url}: {len(votes)} głosowań ({iso}, kadencja {kid})", file=sys.stderr)

    written: list[Path] = []
    for kid, votes in by_kadencja.items():
        payload = {
            "kadencja": kid,
            "source": INDEX_URL,
            "generated_by": "scrape_kk.py --scrape",
            "vote_mode": "faction",
            "total_sessions": len(sessions_per_kad.get(kid, set())),
            "votes": votes,
        }
        out_file = out_dir / f"kadencja-{kid}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        written.append(out_file)

    # Roster aktualnej kadencji z medlemsoversigt (kk.dk). Faction-mode nie
    # opiera profilu radnego na głosach imiennych, ale lista nazwisk + partii
    # jest potrzebna do wyszukiwarki, Google News, OG, breadcrumbs itp.
    try:
        roster = fetch_roster()
        print(f"  roster: {len(roster)} medlemmer", file=sys.stderr)
    except Exception as e:
        print(f"  roster fetch padł: {e}", file=sys.stderr)
        roster = []

    _write_manifest(out_dir, config, by_kadencja, sessions_per_kad, roster=roster)
    return written[0] if written else out_dir / "data.json"


def _write_manifest(
    out_dir: Path,
    config: dict,
    by_kadencja: dict[str, list[dict]],
    sessions_per_kad: dict[str, set[str]],
    roster: list[dict] | None = None,
) -> None:
    """data.json + profiles.json: manifest dla pipeline'u (wzór paris)."""
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    roster = roster or []
    active_kid = config.get("kadencja_active", "")

    # Roster dotyczy kadencji aktywnej; w starszych kadencjach lista jest pusta,
    # bo medlemsoversigt na kk.dk pokazuje tylko bieżący skład. Każdy medlem
    # dostaje listę komisji (udvalg) — wyciąganą z "Medlem af X og Y" przy
    # nazwisku.
    councilors_active = [
        {
            "name": r["name"],
            "slug": r["slug"],
            "club": r["club"],
            "komisje": r.get("komisje", []),
        }
        for r in roster
        if not r.get("is_suppleant")
    ]

    kadencje_payload = []
    for kid, meta in (config.get("kadencje") or {}).items():
        votes = by_kadencja.get(kid, [])
        sessions = sessions_per_kad.get(kid, set())
        kadencje_payload.append({
            "id": kid,
            "label": meta.get("label", kid),
            "total_votes": len(votes),
            "total_sessions": len(sessions),
            "total_councilors": (
                len(councilors_active) if kid == active_kid else 0
            ),
            "councilors": councilors_active if kid == active_kid else [],
        })

    data_payload = {
        "scraped_at": now,
        "generated": True,
        "default_kadencja": active_kid,
        "vote_mode": "faction",
        "kadencje": kadencje_payload,
    }
    if not any(by_kadencja.values()) and not roster:
        data_payload["_status"] = "no_data"
    (out_dir / "data.json").write_text(
        json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # profiles.json: pełna lista radnych (włącznie z suppleantami) dla
    # wyszukiwarki nazwisk i SEO.
    profiles_payload = {
        "scraped_at": now,
        "profiles": [
            {
                "slug": r["slug"],
                "name": r["name"],
                "club": r["club"],
                "kadencja": active_kid,
                "is_suppleant": r.get("is_suppleant", False),
                "is_orlov": r.get("is_orlov", False),
                "profile_url": r.get("profile_url"),
                "kadencje": {
                    active_kid: {
                        "club": r["club"],
                        "komisje": r.get("komisje", []),
                        "has_voting_data": False,
                    }
                },
            }
            for r in roster
        ],
        "total": len(roster),
    }
    (out_dir / "profiles.json").write_text(
        json.dumps(profiles_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── CLI ──────────────────────────────────────────────────────────────
def _load_config() -> dict:
    cfg_path = CITY_DIR / "config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Scraper Borgerrepræsentationen København (kk.dk)")
    ap.add_argument("--scrape", action="store_true",
                    help="pełny scrape (indeks BR + wszystkie punkty)")
    ap.add_argument("--m", metavar="URL",
                    help="scrape jednego posiedzenia (URL referatu)")
    ap.add_argument("--punkt", metavar="URL",
                    help="scrape jednego punktu (debug, wypisuje rekord)")
    ap.add_argument("--limit", type=int, default=None,
                    help="ogranicz liczbę posiedzeń (debug)")
    ap.add_argument("--out", default=str(CITY_DIR / "docs"),
                    help="katalog wyjściowy (domyślnie cities/copenhagen/docs/)")
    args = ap.parse_args()

    config = _load_config()
    out_dir = Path(args.out)

    if args.scrape:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = scrape(out_dir, config, limit_meetings=args.limit)
        print(f"Zapisano: {out_file}")
        return 0

    if args.m:
        out_dir.mkdir(parents=True, exist_ok=True)
        votes, iso = scrape_meeting(args.m, config)
        kid = _kadencja_for_date(iso, config)
        out_file = out_dir / f"kadencja-{kid}.json"
        payload = {
            "kadencja": kid,
            "source": args.m,
            "generated_by": "scrape_kk.py --m",
            "session_date": iso,
            "votes": votes,
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"Sesja {iso}: {len(votes)} głosowań -> {out_file}")
        return 0

    if args.punkt:
        html = fetch_text(args.punkt)
        v = parse_punkt(args.punkt, html, config)
        if v is None:
            print("Punkt nie ma sekcji Beslutning z głosowaniem (uden afstemning lub poza zakresem).")
            return 0
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
