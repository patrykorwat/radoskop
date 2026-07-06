#!/usr/bin/env python3
"""
Generic eSesja scraper library for Radoskop.

eSesja (esesja.pl) is a common BIP CMS used by many Polish municipalities for
publishing session minutes and roll-call votes. URL conventions are stable
across cities, so one parameterised scraper covers any city on the platform.

Usage from a per-city wrapper:

    from lib_esesja import EsesjaScraper

    KADENCJE = {
        "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
    }
    COUNCILORS = {
        # Optional: name -> club mapping. eSesja uses "Lastname Firstname".
        # Without this, club fields are empty but everything else still works.
    }

    EsesjaScraper(
        base_url="https://bytom.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli()  # parses --output/--profiles/--max-sessions/--dry-run/--delay

Source url conventions:
  /glosowania             — paginated session list
  /listaglosowan/{UUID}   — votes in one session
  /glosowanie/{ID}/{HASH} — one vote with named results

Vote page structure:
  <div class='wim'><h3>ZA<span class='za'> (30)</span></h3>
    <div class='osobaa'>Surname FirstName</div>
    ...
  </div>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4 lxml", file=sys.stderr)
    raise

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
    "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5,
    "czerwiec": 6, "lipiec": 7, "sierpień": 8, "wrzesień": 9,
    "październik": 10, "listopad": 11, "grudzień": 12, "styczeń": 1,
}


def parse_polish_date(text: str) -> str | None:
    """Parse '25 Listopada 2024 r.' or '25 Listopada 2024' → '2024-11-25'."""
    text = text.strip().rstrip(".")
    text = re.sub(r"\s*r\.?$", "", text)
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS_PL.get(month_name)
    if not month:
        return None
    return f"{year}-{month:02d}-{day:02d}"


# Kanoniczny slugifier wspólny dla całego projektu (NFKD + overrides dla
# znaków bez dekompozycji kanonicznej jak ł) — patrz lib_slug.py. Stara
# tabela PL dawała identyczne wyniki dla polskich nazwisk, ale nie
# kolabowała separatorów ("Mazur- Kałuża" → podwójny dywiz w slugu); stare
# warianty ratuje _redirects/profiles.json + 301 w workerze.
from lib_slug import make_slug  # noqa: E402, F401
from lib_clubs import club_has_line  # noqa: E402


def build_name_lookup(councilors: dict[str, str]) -> dict[str, str]:
    """Map name (multiple formats) → club. Handles Firstname/Lastname swap."""
    lookup: dict[str, str] = {}
    for name, club in councilors.items():
        lookup[name] = club
        parts = name.split()
        if len(parts) >= 2:
            lookup[f"{parts[-1]} {' '.join(parts[:-1])}"] = club
            lookup[f"{parts[-1]} {parts[0]}"] = club
    return lookup


def compact_named_votes(output: dict) -> dict:
    """Index councilor names per kadencja so vote lists can use ints, not full names."""
    for kad in output.get("kadencje", []):
        names: set[str] = set()
        for v in kad.get("votes", []):
            for cat_names in v.get("named_votes", {}).values():
                for n in cat_names:
                    if isinstance(n, str):
                        names.add(n)
        if not names:
            continue
        index = sorted(names, key=lambda n: n.split()[-1] + " " + n)
        name_to_idx = {n: i for i, n in enumerate(index)}
        kad["councilor_index"] = index
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat in nv:
                nv[cat] = sorted(
                    name_to_idx[n]
                    for n in nv[cat]
                    if isinstance(n, str) and n in name_to_idx
                )
    return output


def save_split_output(output: dict, out_path: Path) -> None:
    """Save data.json (slim index) + kadencja-{id}.json files alongside it.

    `output["kadencje"]` może mieć dwa typy wpisów:
      - **Pełna kadencja** z polem `sessions` (aktualnie scrape'owana) -
        zapisujemy plik `kadencja-{id}.json` + stub w data.json index.
      - **Stub historycznej kadencji** (tylko `id` + `label`, brak `sessions`) -
        idzie tylko do data.json index. Plik `kadencja-{id}.json` NIE jest
        zapisywany (żeby nie nadpisać istniejącego archiwum z poprzednich
        scrape'ów). Stara kadencja pozostaje na S3 nietknięta i SPA fetchuje
        ją normalnie.
    """
    compact_named_votes(output)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        # Tylko pełne kadencje (z sessions) zapisujemy do pliku. Historyczne
        # stub-y zostawiają stary plik nietknięty.
        if kad.get("sessions") is None:
            continue
        kad_path = out_path.parent / f"kadencja-{kid}.json"
        with kad_path.open("w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": output.get("generated", ""),
        "default_kadencja": output.get("default_kadencja", ""),
        "kadencje": stubs,
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


def load_profiles(profiles_path: str | Path) -> dict:
    path = Path(profiles_path)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return {p["name"]: p for p in data.get("profiles", [])}
    except Exception:
        return {}


def _decompact_named_votes(named: dict, councilor_index: list) -> dict:
    """Inverse of compact_named_votes: int indexes → name strings."""
    if not councilor_index:
        return {k: list(v) for k, v in named.items()}
    out: dict[str, list] = {}
    for cat, ids in named.items():
        if ids and isinstance(ids[0], int):
            out[cat] = [councilor_index[i] for i in ids if 0 <= i < len(councilor_index)]
        else:
            out[cat] = list(ids)
    return out


def _is_session_older_than(date_str: str | None, days: int = 2) -> bool:
    """True gdy data sesji YYYY-MM-DD jest co najmniej N dni wstecz.

    Używane przez fetch() do decyzji czy hit'ować disk cache. eSesja może
    dorzucać głosowania post-factum przez pierwszy dzień-dwa, potem treść
    jest stabilna.
    """
    if not date_str:
        return False
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    from datetime import datetime as _dt2
    return (_dt2.now() - dt).days >= days


def load_previous_votes_by_date(
    kadencja_file: Path,
    normalize=None,
) -> dict[tuple[str, str], list[dict]]:
    """Read the previously-saved kadencja JSON and index votes by (date, session_number).

    Returned dict can be indexed in `EsesjaScraper.run()` /
    `BipScraper.run()` to skip re-scraping sessions whose votes are already
    known. Empty dict when file is missing or unreadable, so callers fall
    back to a full scrape.

    Key is the tuple (session_date, session_number). Indexing only by date
    collapsed two sessions on the same date into one bucket and each scrape
    run doubled the votes for that date (Radom 2025-03-31 grew to 36*1024
    over 10 runs). Callers must look up with the matching tuple.

    `normalize`: opcjonalny normalizer nazwisk (np. EsesjaScraper._normalize_name).
    Pliki sprzed fixa 2026-07-06 mają named_votes surowe ("Nazwisko Imię"),
    a roster po swapie, przez co strony głosowań pokazywały odwrócone
    nazwiska i klub "?". Nowe pliki mają flagę `names_normalized` i głosy
    w formie kanonicznej "Imię Nazwisko". Stare leczymy jednorazowo przy
    ładowaniu. Flaga jest konieczna, bo swap nie jest idempotentny:
    zastosowany drugi raz odwróciłby nazwisko z powrotem.
    """
    p = Path(kadencja_file)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            kad = json.load(f)
    except Exception:
        return {}
    if kad.get("names_normalized"):
        normalize = None
    index = kad.get("councilor_index") or []
    by_session: dict[tuple[str, str], list[dict]] = {}
    seen_ids: set[tuple[str, str, str]] = set()
    for v in kad.get("votes") or []:
        date = v.get("session_date") or ""
        if not date:
            continue
        number = v.get("session_number") or ""
        vote_id = v.get("id") or ""
        # Defensive dedup: historical data may contain the exact same
        # (date, number, id) record duplicated 2^N times because of the
        # legacy by-date cache key. Keep first, skip rest.
        dedup_key = (date, number, vote_id)
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)
        nv = v.get("named_votes") or {}
        # Older runs stored full names, newer runs store compact int indexes;
        # decompact transparently so callers always see name strings.
        v_copy = dict(v)
        decompacted = _decompact_named_votes(nv, index)
        # Sanityzacja nazwisk z poprzednich runów: scrape'y sprzed fixa
        # 2026-05-30 zapisały nazwiska z doklejonym tokenem głosu
        # ("Czerner Marian (WSTRZYMAŁ(A) SIĘ)"), przez co Racibórz miał 65
        # "radnych" zamiast 22. Fix w _scrape_single_vote czyści tylko ŚWIEŻE
        # scrape'y, a incremental cache wiecznie odtwarzał zatrute nazwy dla
        # starych sesji. Czyścimy więc przy ładowaniu cache tym samym regexem
        # (no-op dla czystych nazw) — miasta eSesja samonaprawiają się przy
        # zwykłym runie, bez --full.
        def _clean(n: str) -> str:
            n = re.sub(r"\s+", " ", re.sub(r"\s*\(.*\)\s*$", "", n)).strip()
            return normalize(n) if normalize else n

        v_copy["named_votes"] = {
            cat: [_clean(n) for n in names]
            for cat, names in decompacted.items()
        }
        by_session.setdefault((date, number), []).append(v_copy)
    return by_session


def _write_empty_outputs(
    output_path: str | Path,
    profiles_path: str | Path,
    kadencje: dict,
    default_kadencja: str,
) -> None:
    """Write a valid-but-empty trio of files when scrape produced no data.

    Generators downstream (generate_reports.py, generate_main_manifest.py)
    expect specific JSON shapes — missing files cause hard FileNotFoundError
    failures. Emitting empty-but-valid versions lets the pipeline continue.
    """
    out = Path(output_path)
    prof = Path(profiles_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prof.parent.mkdir(parents=True, exist_ok=True)

    kid = default_kadencja
    label = kadencje[kid].get("label", f"Kadencja {kid}")

    # data.json: index pointing at the kadencja stub
    index = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [{"id": kid, "label": label}],
        "_status": "no_data",
    }
    with out.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    # kadencja-{id}.json: empty but well-formed
    kad_stub = {
        "id": kid,
        "label": label,
        "clubs": {},
        "sessions": [],
        "total_sessions": 0,
        "total_votes": 0,
        "total_councilors": 0,
        "councilors": [],
        "votes": [],
        "similarity_top": [],
        "similarity_bottom": [],
    }
    with (out.parent / f"kadencja-{kid}.json").open("w", encoding="utf-8") as f:
        json.dump(kad_stub, f, ensure_ascii=False, separators=(",", ":"))

    # profiles.json: shape `{"profiles": []}` (load_city_data expects dict)
    with prof.open("w", encoding="utf-8") as f:
        json.dump({"profiles": []}, f, ensure_ascii=False, indent=2)


def build_profiles_json(output: dict, profiles_path: str | Path) -> None:
    profiles = []
    for kad in output["kadencje"]:
        kid = kad["id"]
        for c in kad["councilors"]:
            entry = {
                "club": c.get("club", "?"),
                "frekwencja": c.get("frekwencja", 0),
                "aktywnosc": c.get("aktywnosc", 0),
                "zgodnosc_z_klubem": c.get("zgodnosc_z_klubem", 0),
                "votes_za": c.get("votes_za", 0),
                "votes_przeciw": c.get("votes_przeciw", 0),
                "votes_wstrzymal": c.get("votes_wstrzymal", 0),
                "votes_brak": c.get("votes_brak", 0),
                "votes_nieobecny": c.get("votes_nieobecny", 0),
                "votes_total": c.get("votes_total", 0),
                "rebellion_count": c.get("rebellion_count", 0),
                "rebellions": c.get("rebellions", []),
                "has_voting_data": True,
                "has_activity_data": c.get("has_activity_data", False),
                "roles": [],
                "notes": "",
                "former": False,
                "mid_term": False,
            }
            if c.get("activity"):
                entry["activity"] = c["activity"]
            profiles.append({
                "name": c["name"],
                "slug": make_slug(c["name"]),
                "kadencje": {kid: entry},
            })
    path = Path(profiles_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)
    print(f"  Zapisano profiles.json: {len(profiles)} profili")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class EsesjaScraper:
    """Stateful scraper for one eSesja-hosted council.

    Every state previously held in scrape_bialystok module globals lives on
    the instance, so multiple scrapers can run in the same process if needed.
    """

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        base_url: str,
        kadencje: dict,
        councilors: dict | None = None,
        delay: float = 1.0,
        default_kadencja: str | None = None,
        name_order: str = "swap_surname_first",
    ):
        self.base_url = base_url.rstrip("/")
        self.sessions_url = f"{self.base_url}/glosowania"
        self.kadencje = kadencje
        self.councilors = councilors or {}
        self.club_lookup = build_name_lookup(self.councilors)
        # "swap_surname_first": eSesja zwraca "Kowalski Jan" → normalizuj do "Jan Kowalski"
        self.name_order = name_order
        self.delay = delay
        # Default kadencja: the only one that's currently active (no end date).
        # Falls back to the first key in `kadencje`.
        if default_kadencja:
            self.default_kadencja = default_kadencja
        else:
            self.default_kadencja = next(iter(self.kadencje.keys()))
        self._session: requests.Session | None = None
        # HTML disk cache - wire'owane przez --cache-dir w run_cli(). Domyślnie
        # wyłączone żeby zachować backward compat. Pipeline scrape_all.sh
        # przekaże dir dla każdego esesja-based miasta.
        self._cache_dir: Path | None = None

    # -- HTTP layer --------------------------------------------------------

    def _init_session(self) -> None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.UA,
            "Accept-Language": "pl-PL,pl;q=0.9",
        })
        self._session = s

    def init_cache(self, cache_dir: str | None) -> None:
        """Aktywuje disk cache dla HTML responses. Idempotentny."""
        if cache_dir:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._cache_dir = None

    def _cache_path(self, url: str) -> Path | None:
        if self._cache_dir is None:
            return None
        import hashlib
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / f"{h}.html"

    def fetch(self, url: str, use_cache: bool = True) -> BeautifulSoup:
        """Pobiera URL z dyskowym cachem.

        use_cache=False wymusza świeży HTTP (lista sesji, świeże sesje itp).
        Cache key: md5(url)[:16]. Pliki HTML zapisywane do _cache_dir.
        """
        if self._session is None:
            self._init_session()
        cache_file = self._cache_path(url) if use_cache else None
        if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
            text = cache_file.read_text(encoding="utf-8")
            return BeautifulSoup(text, "lxml")
        time.sleep(self.delay)
        print(f"  GET {url}")
        resp = self._session.get(url, timeout=30)  # type: ignore[union-attr]
        resp.raise_for_status()
        # eSesja declares windows-1250 in meta but not HTTP header; requests
        # otherwise falls back to ISO-8859-1 and mangles Polish characters.
        if "esesja" in url:
            resp.encoding = "windows-1250"
        if cache_file:
            try:
                cache_file.write_text(resp.text, encoding="utf-8")
            except Exception:
                pass
        return BeautifulSoup(resp.text, "lxml")

    # -- Name normalisation ------------------------------------------------

    def _normalize_name(self, raw: str) -> str:
        """Opcjonalnie zamienia kolejność "Nazwisko Imię" -> "Imię Nazwisko".

        Aktywne tylko gdy name_order=="swap_surname_first" (eSesja niektórych
        miast zwraca nazwisko przed imieniem). Obsługuje nazwiska złożone
        (np. "Adamczyk-Nowak Beata" -> "Beata Adamczyk-Nowak").
        """
        if self.name_order != "swap_surname_first" or not raw:
            return raw
        parts = raw.split()
        if len(parts) < 2:
            return raw
        # eSesja podaje "NAZWISKO Imię [Imię2]": pierwszy token to nazwisko
        # (złożone nazwiska są łączone dywizem, więc to nadal jeden token),
        # reszta to imiona. Bierzemy nazwisko z przodu i przenosimy na koniec,
        # zachowując kolejność imion. Dzięki temu radny z dwoma imionami
        # ("Chmielewski Adam Łukasz") wychodzi poprawnie jako
        # "Adam Łukasz Chmielewski", a nie "Łukasz Chmielewski Adam".
        swapped = parts[1:] + [parts[0]]
        # Część instancji eSesja zwraca nazwisko WERSALIKAMI ("BRÓZDA Sebastian").
        # Po swapie zostałoby "Sebastian BRÓZDA" — wciąż wygląda na zepsute.
        # Normalizujemy tylko tokeny pisane w całości wielkimi literami; tokeny
        # już o mieszanej wielkości ("Dobrowolska-Cylwik") zostają nietknięte.
        # str.title() poprawnie obsługuje dywiz ("CZAJA-DOROSZUK" -> "Czaja-Doroszuk").
        norm = [t.title() if t.isupper() and len(t) >= 2 else t for t in swapped]
        return " ".join(norm)

    # -- Club resolution ---------------------------------------------------

    def resolve_club(self, name: str) -> str:
        if not name:
            return ""
        if name in self.club_lookup:
            return self.club_lookup[name]
        # eSesja części miast zwraca NAZWISKO wielkimi literami i/lub w kolejności
        # "Nazwisko Imię", a club_assignments trzyma "Imię Nazwisko" title-case
        # (np. "ROŻNIATOWSKI Arkadiusz" vs "Arkadiusz Rożniatowski"). Dlatego
        # dopasowujemy niewrażliwie na wielkość liter.
        ncf = name.casefold()
        for key, club in self.club_lookup.items():
            if key.casefold() == ncf:
                return club
        # Dopasowanie po zbiorze tokenów: ten sam radny gdy nazwa i klucz dzielą
        # oba główne tokeny (nazwisko + imię). Odporne na kolejność, wielkość
        # liter i drugie imię; wymaga 2 wspólnych tokenów, więc samo wspólne imię
        # nie powoduje fałszywego trafienia.
        nm = {t.casefold() for t in name.split()}
        if not nm:
            return ""
        for key, club in self.club_lookup.items():
            kt = {t.casefold() for t in key.split()}
            if nm == kt or len(nm & kt) >= 2:
                return club
        return ""

    # -- Step 1: session list ----------------------------------------------

    def scrape_session_list(self) -> list[dict]:
        sessions: list[dict] = []
        seen_urls: set[str] = set()
        page = 1
        while True:
            url = self.sessions_url if page == 1 else f"{self.sessions_url}/{page}"
            try:
                # Lista sesji ZAWSZE fresh - musimy wykryć nowe sesje na BIP.
                soup = self.fetch(url, use_cache=False)
            except Exception as e:
                print(f"  Nie udalo sie pobrac {url}: {e}")
                break

            new_unique_on_page = 0  # zliczamy wszystkie unikalne /listaglosowan/
            page_had_links = False
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/listaglosowan/" not in href:
                    continue
                page_had_links = True
                text = a.get_text(strip=True)
                m = re.search(r"w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text)
                if not m:
                    continue
                full_url = href if href.startswith("http") else self.base_url + href
                # Dedup wszystkie posiedzenia (sesje + komisje), żeby paginacja
                # wykrywała stop point (eSesja repeatuje stronę dla page > max).
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                new_unique_on_page += 1
                # Filtr typu posiedzenia: w listingu /glosowania eSesja zwraca
                # WSZYSTKIE posiedzenia (sesje rady + posiedzenia komisji +
                # wspólne posiedzenia). Sesje rady ZAWSZE zaczynają się od
                # "Sesja" (np. "Sesja Rady Miasta Bytomia", "Sesja nr XIV",
                # "Sesja Nadzwyczajna"). Posiedzenia komisji zaczynają się od
                # "Komisja X", "Posiedzenie Komisji", "Wspólne posiedzenie".
                # Bez tego filtra Radoskop pokazywał komisje jako sesje rady,
                # zaniżając frekwencję (komisja ma 4 obecnych zamiast 21).
                # Dedup ZAWSZE wcześniej, paginacja musi widzieć komisje jako
                # zobaczone, inaczej zatrzyma się na pierwszej stronie samych
                # komisji (np. Katowice — pierwsza strona ma 0 sesji).
                # Sesja rady vs komisja/inne posiedzenie. Tytuł sesji zawiera
                # słowo "sesja", CZĘSTO z numerem rzymskim na początku
                # ("XXII sesja", "II Sesja", "XXII nadzwyczajna sesja"). Stary
                # filtr `first_word == "sesja"` odrzucał takie sesje — dla części
                # eSesji (bransk/walbrzych/kobylka/reda/debica/lubawa...)
                # przechodziła tylko inauguracyjna "Sesja Rady", więc dane były
                # zamrożone na maju 2024. Teraz: akceptuj gdy tytuł zawiera rdzeń
                # "sesj", a odrzucaj komisje i inne posiedzenia.
                low = text.strip().lower()
                if "komisj" in low or low.startswith(
                    ("posiedzenie", "wspólne", "wspolne", "konwent", "spotkanie", "narada", "debata")
                ):
                    continue
                if "sesj" not in low:
                    continue
                day = int(m.group(1))
                month = MONTHS_PL.get(m.group(2).lower())
                year = int(m.group(3))
                if not month:
                    continue
                date_str = f"{year}-{month:02d}-{day:02d}"
                nr_match = re.search(r"nr\s+([IVXLCDM]+)", text)
                session_number = nr_match.group(1) if nr_match else ""
                sessions.append({
                    "id": full_url.split("/")[-1],
                    "date": date_str,
                    "number": session_number,
                    "url": full_url,
                    "title": text,
                })

            # Stop conditions (in order):
            #  1. strona w ogóle nie miała /listaglosowan/ → koniec paginacji
            #     (np. eSesja renderuje pustą stronę dla pageN gdzie N > max).
            #  2. wszystkie /listaglosowan/ na stronie to DUPLIKATY już
            #     zebranych — wcześniejszy bug: dla tarnow/walbrzych/elblag
            #     paginacja /glosowania/N+1 redirectuje do tej samej zawartości
            #     co poprzednie strony zamiast 404, więc scraper musiał liczyć
            #     unikalne URL żeby się zatrzymać.
            #  3. twarda granica 50 stron żeby uciec od edge cases.
            if not page_had_links or new_unique_on_page == 0:
                break
            if page >= 50:
                break
            page += 1

        if not sessions:
            print("  UWAGA: Nie znaleziono sesji!")
            return []

        seen: set[str] = set()
        unique = []
        for s in sessions:
            if s["url"] not in seen:
                seen.add(s["url"])
                unique.append(s)

        kadencja_start = self.kadencje[self.default_kadencja]["start"]
        filtered = [s for s in unique if s["date"] >= kadencja_start]
        print(
            f"  Znaleziono {len(unique)} sesji ogolnie, "
            f"{len(filtered)} w kadencji {self.default_kadencja}"
        )
        return sorted(filtered, key=lambda x: x["date"])

    # -- Step 2: votes per session -----------------------------------------

    def scrape_votes_from_session(self, session: dict) -> list[dict]:
        votes: list[dict] = []
        # Cache HTML sesji tylko jeśli starsza niż 2 dni (głosowania finalne).
        # Świeższe sesje (do 2 dni) wymuszają fresh HTTP bo eSesja może
        # dorzucać głosowania post-factum.
        is_stable = _is_session_older_than(session.get("date"), days=2)
        try:
            soup = self.fetch(session["url"], use_cache=is_stable)
        except Exception as e:
            print(f"    Blad pobierania sesji: {e}")
            return votes

        seen_urls: set[str] = set()
        vote_links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/glosowanie/" not in href or "/listaglosowan/" in href:
                continue
            url = href if href.startswith("http") else self.base_url + href
            if url in seen_urls:
                continue
            seen_urls.add(url)
            vote_links.append(url)

        print(f"    Znaleziono {len(vote_links)} linkow do glosowan")

        for idx, url in enumerate(vote_links):
            vote = self._scrape_single_vote(url, session, idx)
            if vote:
                votes.append(vote)
            time.sleep(self.delay * 0.5)

        print(f"    Wyodrebniono {len(votes)} glosowan z imiennymi wynikami")
        return votes

    def _scrape_single_vote(self, url: str, session: dict, vote_idx: int) -> dict | None:
        # Per-głosowanie page: cache jeśli sesja jest stabilna (>2 dni).
        is_stable = _is_session_older_than(session.get("date"), days=2)
        try:
            soup = self.fetch(url, use_cache=is_stable)
        except Exception as e:
            print(f"      Blad pobierania {url}: {e}")
            return None

        h1 = soup.find("h1")
        topic = h1.get_text(strip=True)[:500] if h1 else ""
        topic = re.sub(r"^Wyniki głosowania jawnego w sprawie:\s*", "", topic).strip()
        topic = re.sub(r"^Wyniki głosowania w sprawie:?\s*", "", topic).strip()
        topic = re.sub(r"^Głosowanie\s+w\s+sprawie\s+", "", topic).strip()
        if not topic:
            topic = f"Glosowanie {vote_idx + 1}"

        named_votes: dict[str, list[str]] = {
            "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": [],
        }
        category_map = {
            "za": "za",
            "przeciw": "przeciw",
            "wstrzymuj": "wstrzymal_sie",
            "brak g": "brak_glosu",
            "nieobecn": "nieobecni",
        }
        for wim in soup.find_all("div", class_="wim"):
            h3 = wim.find("h3")
            if not h3:
                continue
            h3_text = h3.get_text(strip=True).upper()
            cat_key = None
            for prefix, key in category_map.items():
                if h3_text.upper().startswith(prefix.upper()):
                    cat_key = key
                    break
            if not cat_key:
                continue
            for osoba in wim.find_all("div", class_="osobaa"):
                name = osoba.get_text(strip=True)
                # Niektóre instancje eSesja (np. Racibórz) doklejają token głosu
                # w nawiasie do nazwy w div.osobaa, np. "Dutkiewicz Katarzyna
                # (NIE)" albo "Czerner Marian (WSTRZYMAŁ(A) SIĘ)". Kategorię i tak
                # bierzemy z nagłówka h3, więc obcinamy końcowy nawias — inaczej
                # ten sam radny tworzy wiele wpisów w rosterze (Racibórz: 65 zamiast
                # 22). Zachłannie, bo token bywa zagnieżdżony "(WSTRZYMAŁ(A) SIĘ)".
                # Dla miast bez tokenu w nazwie to no-op (brak nawiasu na końcu).
                name = re.sub(r"\s*\(.*\)\s*$", "", name).strip()
                if name and len(name) > 2:
                    named_votes[cat_key].append(name)

        if sum(len(v) for v in named_votes.values()) == 0:
            return None

        counts = {cat: len(named_votes[cat]) for cat in named_votes}
        # Include session number in vote_id so two sessions on the same date
        # do not collide on the same key (Radom XXI vs XXII both on
        # 2025-03-31 case).
        session_num = session.get("number", "") or ""
        num_part = f"_{session_num}" if session_num else ""
        return {
            "id": f"{session['date']}{num_part}_{vote_idx:03d}_000",
            "source_url": url,
            "session_date": session["date"],
            "session_number": session.get("number", ""),
            "topic": topic[:500],
            "druk": None,
            "resolution": None,
            "counts": counts,
            "named_votes": named_votes,
        }

    # -- Step 3: aggregations ---------------------------------------------

    def build_councilors(
        self,
        all_votes: list[dict],
        sessions: list[dict],
        existing_profiles: dict,
    ) -> list[dict]:
        stats: dict[str, dict] = defaultdict(lambda: {
            "name": "", "club": "", "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
            "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
            "rebellion_count": 0, "rebellions": [],
            "has_voting_data": True, "has_activity_data": False,
        })

        # named_votes są już w formie kanonicznej "Imię Nazwisko" (run()
        # normalizuje świeże głosy, loader leczy cache). NIE wolno tu wołać
        # _normalize_name drugi raz: swap nie jest idempotentny i odwróciłby
        # nazwiska z powrotem.
        for vote in all_votes:
            for cat, names in vote["named_votes"].items():
                for name in names:
                    stats[name]["name"] = name
                    stats[name]["club"] = self.resolve_club(name)
                    stats[name]["votes_total"] += 1
                    if cat == "za":
                        stats[name]["votes_za"] += 1
                    elif cat == "przeciw":
                        stats[name]["votes_przeciw"] += 1
                    elif cat == "wstrzymal_sie":
                        stats[name]["votes_wstrzymal"] += 1
                    elif cat == "brak_glosu":
                        stats[name]["votes_brak"] += 1

        for _, s in stats.items():
            if s["votes_total"] > 0:
                s["frekwencja"] = round(
                    (s["votes_total"] - s["votes_brak"]) / s["votes_total"] * 100, 1
                )
                s["aktywnosc"] = round(
                    (s["votes_za"] + s["votes_przeciw"] + s["votes_wstrzymal"])
                    / s["votes_total"] * 100, 1
                )

        # Zgodność z klubem + bunty. Liczone tak jak build_assembly_metrics
        # (flat-schema sejmiki): per głosowanie większość klubu z aktywnych
        # głosów (za/przeciw/wstrzymal_sie), bunt = głos wbrew większości
        # swojego klubu, zgodnosc = (aktywne - bunty) / aktywne. Radni bez
        # rozpoznanego klubu (resolve_club == "") nie mają większości, więc nie
        # buntują się i mają 100%.
        active_count: dict[str, int] = defaultdict(int)
        for vote in all_votes:
            decision_of: dict[str, str] = {}
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                for name in vote["named_votes"].get(cat, []):
                    decision_of[name] = cat
            club_dec: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for name, dec in decision_of.items():
                club = stats[name]["club"]
                # Niezrzeszeni (kod "NZ" / literał) i bez klubu nie mają linii —
                # nie liczymy dla nich większości, więc nie buntują się.
                if club_has_line(club):
                    club_dec[club][dec] += 1
            club_major = {
                club: max(dc.items(), key=lambda kv: kv[1])[0]
                for club, dc in club_dec.items() if dc
            }
            for name, dec in decision_of.items():
                active_count[name] += 1
                club = stats[name]["club"]
                if club in club_major and dec != club_major[club]:
                    stats[name]["rebellion_count"] += 1
                    stats[name]["rebellions"].append({
                        "session": vote.get("session_date"),
                        "topic": (vote.get("topic") or "")[:200],
                        "their_vote": dec,
                        "club_majority": club_major[club],
                    })
        for name, s in stats.items():
            active = active_count.get(name, 0)
            if active > 0:
                s["zgodnosc_z_klubem"] = round(
                    (active - s["rebellion_count"]) / active * 100, 1
                )

        result = []
        for name, s in sorted(stats.items()):
            # existing_profiles (load_profiles) jest kluczowane nazwą wyświetlaną
            # = znormalizowaną (s["name"]). stats jest kluczowane nazwą surową.
            # Szukamy po znormalizowanej, inaczej dla miast ze swapem nazwisk
            # wzbogacenie z poprzedniego runu (activity itd.) nigdy się nie łączy.
            disp = s.get("name") or name
            prof = existing_profiles.get(disp) or existing_profiles.get(name)
            if prof:
                s.update({k: v for k, v in prof.items() if k not in s or not s[k]})
            result.append(s)
        return result

    def compute_similarity(self, all_votes: list[dict], councilors: list[dict]) -> tuple[list, list]:
        # named_votes są kanoniczne "Imię Nazwisko" (normalizacja w run() +
        # leczenie cache w loaderze), więc klucze wektorów składają się z
        # rosterem (name_to_club, klucz c["name"]) bez dodatkowego swapu.
        # NIE wolno tu wołać _normalize_name: nie jest idempotentne.
        name_to_club = {c["name"]: c.get("club", "?") for c in councilors}
        vectors: dict[str, dict] = defaultdict(dict)
        for v in all_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie"]:
                for name in v["named_votes"].get(cat, []):
                    vectors[name][v["id"]] = cat

        names = sorted(vectors.keys())
        pairs = []
        for a, b in combinations(names, 2):
            common = set(vectors[a].keys()) & set(vectors[b].keys())
            if len(common) < 10:
                continue
            same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
            score = round(same / len(common) * 100, 1)
            pairs.append({
                "a": a, "b": b,
                "club_a": name_to_club.get(a, "?"),
                "club_b": name_to_club.get(b, "?"),
                "score": score,
                "common_votes": len(common),
            })
        pairs.sort(key=lambda x: x["score"], reverse=True)
        return pairs[:20], pairs[-20:][::-1]

    def build_sessions(self, sessions_raw: list[dict], all_votes: list[dict]) -> list[dict]:
        votes_by_date: dict[str, list[dict]] = defaultdict(list)
        for v in all_votes:
            votes_by_date[v["session_date"]].append(v)
        result = []
        for s in sessions_raw:
            date = s["date"]
            session_votes = votes_by_date.get(date, [])
            # named_votes są kanoniczne "Imię Nazwisko" (normalizacja w run()
            # + leczenie cache w loaderze), zgodne z rosterem build_councilors.
            # NIE wolno tu wołać _normalize_name (brak idempotencji): podwójny
            # swap dałby zero trafień z rosterem i absent = cały roster
            # (fałszywe "X z Y ław pustych").
            attendees: set[str] = set()
            absent_marked: set[str] = set()
            for v in session_votes:
                for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                    for n in v["named_votes"].get(cat, []):
                        attendees.add(n)
                for n in v["named_votes"].get("nieobecni", []):
                    absent_marked.add(n)
            # Obecność wygrywa: kto choć raz głosował/był w quorum, nie jest
            # nieobecny, nawet jeśli pojedyncze głosowanie minął. To też wyklucza
            # byłych radnych z innych sesji — bierzemy tylko nieobecnych
            # zarejestrowanych przez samą radę dla TEJ sesji.
            absent = sorted(absent_marked - attendees)
            # eSesja's session listing typically doesn't expose a stable number.
            # Without a fallback the per-city template generates /sesja// links.
            # Use date as the URL slug — every session has one.
            number = s.get("number", "") or date
            result.append({
                "date": date,
                "number": number,
                "vote_count": len(session_votes),
                "attendee_count": len(attendees),
                "attendees": sorted(attendees),
                "absent_names": absent,
                "speakers": [],
            })
        return sorted(result, key=lambda x: x["date"])

    # -- Top-level run -----------------------------------------------------

    def run(
        self,
        output_path: str | Path,
        profiles_path: str | Path,
        max_sessions: int = 0,
        dry_run: bool = False,
        incremental_window_days: int = 30,
        force_full: bool = False,
    ) -> int:
        self._init_session()
        slug = self.base_url.split("//", 1)[-1].split(".", 1)[0]
        print(f"\n=== Radoskop {slug} — eSesja scraper ===\n")

        print("[1/4] Pobieranie listy sesji...")
        sessions = self.scrape_session_list()
        if not sessions:
            print("UWAGA: Nie znaleziono sesji — zapisuję pusty wynik.")
            _write_empty_outputs(output_path, profiles_path, self.kadencje, self.default_kadencja)
            return 0
        if max_sessions > 0:
            sessions = sessions[:max_sessions]
        print(f"  Znaleziono {len(sessions)} sesji\n")

        if dry_run:
            print("Dry-run: Zatrzymuję się tutaj.")
            return 0

        # Load previously-saved votes and re-scrape only sessions newer than
        # the safety window (defaults to 30d). Older sessions are immutable
        # in practice. Votes register minutes after a session ends and
        # corrections, when they happen, land within the first weeks.
        prev_votes_by_date: dict[tuple[str, str], list[dict]] = {}
        if not force_full:
            kad_file = Path(output_path).parent / f"kadencja-{self.default_kadencja}.json"
            prev_votes_by_date = load_previous_votes_by_date(
                kad_file, normalize=self._normalize_name
            )
            if prev_votes_by_date:
                print(
                    f"  Cache: {sum(len(v) for v in prev_votes_by_date.values())} "
                    f"głosowań z poprzedniego runu ({len(prev_votes_by_date)} sesji)"
                )

        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=incremental_window_days)).isoformat()
        print(f"  Incremental window: re-scrape sesji od {cutoff} (granica {incremental_window_days} dni)")

        print("[2/4] Pobieranie głosowań z sesji...")
        all_votes: list[dict] = []
        fresh_count = 0
        cached_count = 0
        for i, session in enumerate(sessions):
            # Cache key matches load_previous_votes_by_date: (date, session_number).
            # Two sessions same date with by-date-only key doubled votes each run.
            cache_key = (session["date"], session.get("number", "") or "")
            cached = prev_votes_by_date.get(cache_key)
            if cached and session["date"] < cutoff:
                print(f"  [{i+1}/{len(sessions)}] CACHED Sesja {session['date']} ({len(cached)} głosowań)")
                all_votes.extend(cached)
                cached_count += len(cached)
            else:
                print(f"  [{i+1}/{len(sessions)}] Sesja {session['id']} ({session['date']})")
                fresh = self.scrape_votes_from_session(session)
                # Kanoniczna forma "Imię Nazwisko" już przy zapisie, nie przy
                # odczycie. Jedno miejsce dla obu ścieżek: _scrape_single_vote
                # oraz override'y subklas (Wałbrzych buduje named_votes z
                # PDF-ów). Wcześniej named_votes szły do pliku surowe
                # ("Nazwisko Imię"), a roster po swapie, więc strony głosowań
                # (SPA, prerender, data_api club_map) pokazywały odwrócone
                # nazwiska i klub "?" dla wszystkich radnych.
                for v in fresh:
                    nv = v.get("named_votes") or {}
                    v["named_votes"] = {
                        cat: [self._normalize_name(n) for n in names]
                        for cat, names in nv.items()
                    }
                all_votes.extend(fresh)
                fresh_count += len(fresh)
        # Defensive dedup by (date, number, id). Cleans historical duplicates
        # from the legacy by-date cache bug on first run after the fix.
        seen_keys: set[tuple[str, str, str]] = set()
        deduped: list[dict] = []
        for v in all_votes:
            key = (
                v.get("session_date") or "",
                v.get("session_number") or "",
                v.get("id") or "",
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(v)
        dropped = len(all_votes) - len(deduped)
        if dropped:
            print(f"  Dedup: usunieto {dropped} duplikatow (legacy by-date cache bug)")
        all_votes = deduped
        print(f"  Pobrano {fresh_count} fresh + {cached_count} cached = {len(all_votes)} głosowań\n")

        print("[3/4] Budowanie danych...")
        existing_profiles = load_profiles(profiles_path)
        councilors = self.build_councilors(all_votes, sessions, existing_profiles)
        sessions_data = self.build_sessions(sessions, all_votes)
        sim_top, sim_bottom = self.compute_similarity(all_votes, councilors)

        club_counts: dict[str, int] = defaultdict(int)
        for c in councilors:
            club_counts[c["club"]] += 1

        print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
        print(f"  Kluby: {dict(club_counts)}\n")

        kid = self.default_kadencja
        kad_output = {
            "id": kid,
            "label": self.kadencje[kid]["label"],
            # named_votes w formie kanonicznej "Imię Nazwisko". Loader
            # (load_previous_votes_by_date) pomija leczenie swapem gdy flaga
            # jest ustawiona; bez niej podwójny swap odwracałby nazwiska.
            "names_normalized": True,
            "clubs": {club: count for club, count in sorted(club_counts.items())},
            "sessions": sessions_data,
            "total_sessions": len(sessions_data),
            "total_votes": len(all_votes),
            "total_councilors": len(councilors),
            "councilors": councilors,
            "votes": all_votes,
            "similarity_top": sim_top,
            "similarity_bottom": sim_bottom,
        }
        # Stub'y dla historycznych kadencji znanych z config, ale których
        # nie scrape'ujemy w bieżącym runie. Idą tylko do data.json index
        # (save_split_output nie zapisze pliku kadencja-{id}.json bo brak
        # `sessions`). Archiwum na S3 z poprzednich scrape'ów pozostaje
        # nietknięte. Dzięki temu SPA wie że stara kadencja istnieje i
        # fetchuje ją po wyborze z menu kadencji.
        historical_stubs = []
        for hist_kid, hist_meta in self.kadencje.items():
            if hist_kid == kid:
                continue
            historical_stubs.append({
                "id": hist_kid,
                "label": hist_meta.get("label", f"Kadencja {hist_kid}"),
            })

        output = {
            "generated": datetime.now().isoformat(),
            "default_kadencja": kid,
            "kadencje": [kad_output] + historical_stubs,
        }

        print("[4/4] Zapisywanie danych...")
        out_path = Path(output_path)
        save_split_output(output, out_path)
        print(f"Gotowe! Zapisano do {out_path}")
        print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych\n")

        build_profiles_json(output, profiles_path)
        return 0

    def run_cli(self, prog_name: str | None = None) -> int:
        """Wrap run() with the standard --output/--profiles/--max-sessions CLI."""
        parser = argparse.ArgumentParser(description=prog_name or "eSesja scraper")
        parser.add_argument("--output", default="docs/data.json")
        parser.add_argument("--profiles", default="docs/profiles.json")
        parser.add_argument("--max-sessions", type=int, default=0)
        parser.add_argument("--delay", type=float, default=0.3)
        parser.add_argument("--dry-run", action="store_true")
        # HTML disk cache: lista sesji zawsze fresh (bez cache), strony sesji
        # i głosowań starszych niż 2 dni hitują cache (eSesja nie zmienia
        # historycznych protokołów). Pipeline przekazuje scratch_dir/.cache/html.
        parser.add_argument("--cache-dir", default=None)
        parser.add_argument(
            "--incremental-window", type=int, default=30,
            help="Re-scrape sessions newer than N days (default 30); older sessions reuse cached votes",
        )
        parser.add_argument(
            "--full", action="store_true",
            help="Force full re-scrape, ignoring previous kadencja JSON",
        )
        args = parser.parse_args()
        if args.delay != 0.3:
            self.delay = args.delay
        self.init_cache(args.cache_dir)
        return self.run(
            incremental_window_days=args.incremental_window,
            force_full=args.full,
            output_path=args.output,
            profiles_path=args.profiles,
            max_sessions=args.max_sessions,
            dry_run=args.dry_run,
        )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def make_scraper(
    base_url: str,
    kadencje: dict,
    councilors: dict | None = None,
    delay: float = 1.0,
) -> EsesjaScraper:
    return EsesjaScraper(base_url, kadencje, councilors, delay=delay)
