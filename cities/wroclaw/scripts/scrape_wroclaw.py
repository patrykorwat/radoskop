# Źródło: config.json → club_assignments (jedno źródło prawdy)
def _load_councilors() -> dict[str, str]:
    """Load club assignments from config.json (single source of truth)."""
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            return cfg.get("club_assignments", {}) or {}
        except Exception:
            pass
    return {}

COUNCILORS = _load_councilors()

# Reusable HTTP session
_session = None


def init_session():
    """Create a requests session with proper headers."""
    global _session
    _session = requests.Session()
    _session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9",
    })


_CACHE_DIR: Path | None = None

# Sesje plenarne starsze niż tyle dni traktujemy jako stabilne. BIP nie
# modyfikuje protokołów/wyników po finalizacji posiedzenia (kilka dni max).
STABLE_AGE_DAYS = 2  # sesje >=2 dni cache, bo vote data zwykle publikowany w 24h


def init_cache(cache_dir: str | None) -> None:
    """Ustawia katalog cache HTML. None = wyłącz cache (legacy behavior)."""
    global _CACHE_DIR
    if cache_dir:
        _CACHE_DIR = Path(cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    else:
        _CACHE_DIR = None


def _cache_path(url: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    import hashlib
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.html"


def _is_session_stable(date_str: str) -> bool:
    """True gdy data sesji YYYY-MM-DD jest >= STABLE_AGE_DAYS dni temu."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    age = (datetime.now() - dt).days
    return age >= STABLE_AGE_DAYS


def fetch(url: str, use_cache: bool = True) -> BeautifulSoup:
    """Fetch a page (with disk cache) and return BeautifulSoup.

    use_cache=False wymusza świeży HTTP request (np. dla list sesji lub świeżych
    sesji których zawartość może się zmienić).
    """
    cache_file = _cache_path(url) if use_cache else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        return BeautifulSoup(cache_file.read_text(encoding="utf-8"), "lxml")

    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    if cache_file:
        try:
            cache_file.write_text(resp.text, encoding="utf-8")
        except Exception:
            pass
    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# Polish month name → number mapping
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
    # Remove trailing 'r' or 'r.'
    text = re.sub(r'\s*r\.?$', '', text)
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS_PL.get(month_name)
    if not month:
        return None
    return f"{year}-{month:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# Step 1: Scrape session list
# ---------------------------------------------------------------------------

def _extract_sessions_from_soup(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract session info from a BeautifulSoup page."""
    sessions = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]

        # Match text: "...nr XI dnia 21 listopada 2024 r. godz. 11:00"
        m = re.search(
            r'nr\s+([IVXLCDM]+)\s+dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})',
            text,
            re.IGNORECASE
        )
        if not m:
            # Try matching from href slug:
            # "sesja-rady-miejskiej-wroclawia-nr-ix-dnia-17-pazdziernika-2024-r-godz-11-00"
            slug = href.split("/")[-1] if "/" in href else ""
            m_href = re.search(
                r'nr-([ivxlcdm]+)-dnia-(\d{1,2})-(\w+)-(\d{4})',
                slug,
                re.IGNORECASE
            )
            if m_href:
                number = m_href.group(1).upper()
                day = int(m_href.group(2))
                month_name = m_href.group(3).lower()
                year = int(m_href.group(4))
            else:
                continue
        else:
            number = m.group(1).upper()
            day = int(m.group(2))
            month_name = m.group(3).lower()
            year = int(m.group(4))

        month = MONTHS_PL.get(month_name)
        if not month:
            continue
        date = f"{year}-{month:02d}-{day:02d}"

        if not href.startswith("http"):
            href = urljoin(base_url, href)

        sessions.append({
            "number": number,
            "date": date,
            "url": href,
        })
    return sessions


def _fetch_paginated(base_url: str, stop_before_date: str | None = None) -> list[dict]:
    """Fetch a BIP listing page + all pagination pages, extract sessions.

    Strona 1 (base_url) jest zawsze pobierana świeżo. Strony 2+ są cachowane.

    Paginacja następuje wyłącznie przez link "następna" (nie przez numerowane linki
    w nawigacji — BIP Wrocław ma tysiące numerycznych linków do dokumentów, które
    wyglądają jak numery stron i powodują pobranie 2000+ zbędnych stron).

    stop_before_date: YYYY-MM-DD — przerywa paginację gdy wszystkie sesje na stronie
    są starsze niż ta data. Pozwala zatrzymać się po znalezieniu wszystkich sesji
    bieżącej kadencji bez przeglądania historycznych stron.
    """
    sessions = []
    visited = set()
    current_url = base_url
    is_first_page = True

    while current_url:
        if current_url in visited:
            break
        visited.add(current_url)

        try:
            soup = fetch(current_url, use_cache=not is_first_page)
        except Exception as e:
            print(f"  Nie udało się pobrać {current_url}: {e}")
            break

        page_sessions = _extract_sessions_from_soup(soup, base_url)
        sessions.extend(page_sessions)
        is_first_page = False

        # Early stop: if all sessions on this page predate the cutoff, we are past
        # the kadencja we care about — no need to keep paging backward in time.
        if stop_before_date and page_sessions:
            if all(s["date"] < stop_before_date for s in page_sessions):
                break

        # Find the "następna" / next link to continue pagination.
        # Deliberately NOT following numbered links — BIP Wrocław embeds
        # thousands of numeric document-ID links that look like page numbers.
        current_url = None
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() in ("następna", "»", ">", "next", "→"):
                candidate = urljoin(base_url, a["href"])
                if candidate not in visited:
                    current_url = candidate
                    break

    return sessions


def scrape_session_list() -> list[dict]:
    """Fetch session list from BIP Wrocław.

    Tries multiple category URLs (IX kadencja = 1179, VIII kadencja = 769)
    with pagination support.
    """
    sessions = []
    kadencja_start = KADENCJE["2024-2029"]["start"]

    for url in SESSIONS_URLS:
        print(f"  Próbuję: {url}")
        page_sessions = _fetch_paginated(url, stop_before_date=kadencja_start)
        if page_sessions:
            print(f"    → znaleziono {len(page_sessions)} sesji")
            sessions.extend(page_sessions)
            break  # IX kadencja found — skip VIII kadencja fallback

    if not sessions:
        print("  UWAGA: Nie znaleziono sesji na żadnej stronie!")
        return []

    # Deduplicate by (number, date) — different kadencje may reuse Roman numerals
    seen = set()
    unique = []
    for s in sessions:
        key = (s["number"], s["date"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    # Filter by kadencja — only sessions from 2024-05-07 onwards
    filtered = [s for s in unique if s["date"] >= kadencja_start]
    print(f"  Znaleziono {len(unique)} sesji ogółem, {len(filtered)} w kadencji 2024-2029")

    if not filtered and unique:
        print(f"  UWAGA: Brak sesji po {kadencja_start}.")
        print(f"  Najnowsza znaleziona: {max(s['date'] for s in unique)}")
        # Return all — user can filter manually with --all-kadencje
        return sorted(unique, key=lambda x: x["date"])

    return sorted(filtered, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Step 2: Scrape session page → find PDF links
# ---------------------------------------------------------------------------

def scrape_session_pdf_links(session: dict) -> list[dict]:
    """Fetch session page and find vote result PDF attachment links.

    BIP Wrocław has 3 types of attachments per druk:
      - "Projekt uchwały druk nr XXXX/YY"  (draft resolution — skip)
      - "Uzasadnienie druk nr XXXX/YY"     (justification — skip)
      - "Wynik głosowania druk nr XXXX/YY" (vote result — KEEP!)

    We only download "Wynik głosowania" attachments.

    Stabilne sesje (date > 7 dni temu) używają cache HTML.
    """
    is_stable = _is_session_stable(session.get("date", ""))
    soup = fetch(session["url"], use_cache=is_stable)
    vote_links = []
    total_attachments = 0

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        # Look for attachments/download links
        if "/attachments/download/" not in href:
            continue

        total_attachments += 1

        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        m = re.search(r'/attachments/download/(\d+)', href)
        if not m:
            continue

        att_id = m.group(1)

        # FILTER: Only keep "Wynik głosowania" links
        text_lower = text.lower()
        is_vote = (
            "wynik głosowania" in text_lower
            or "wyniki głosowania" in text_lower
            or "głosowanie imienne" in text_lower
            or "głosowanie nr" in text_lower
        )

        if is_vote:
            # Extract druk number if present
            druk_match = re.search(r'druk\s+nr\s+(\S+)', text, re.IGNORECASE)
            druk_nr = druk_match.group(1) if druk_match else None

            vote_links.append({
                "url": href,
                "text": text,
                "att_id": att_id,
                "druk": druk_nr,
            })

    # Deduplicate by attachment ID
    seen_att = set()
    unique_links = []
    for pl in vote_links:
        if pl["att_id"] not in seen_att:
            seen_att.add(pl["att_id"])
            unique_links.append(pl)

    print(f"    ({total_attachments} załączników ogółem, {len(unique_links)} wyników głosowań)")
    return unique_links


# ---------------------------------------------------------------------------
# Step 3: Download and parse PDF
# ---------------------------------------------------------------------------

def download_pdf(pdf_url: str, cache_dir: Path) -> Path | None:
    """Download a PDF from URL to cache directory."""
    # Extract ID from URL for filename
    m = re.search(r'/attachments/download/(\d+)', pdf_url)
    if not m:
        return None

    att_id = m.group(1)
    filename = f"protokol_{att_id}.pdf"
    path = cache_dir / filename

    if path.exists() and path.stat().st_size > 1000:
        print(f"    Cache hit: {filename}")
        return path

    time.sleep(DELAY)
    print(f"    GET {pdf_url}")
    try:
        resp = _session.get(pdf_url, timeout=60)
        resp.raise_for_status()
        # Verify we got a PDF
        if b"%PDF" not in resp.content[:10]:
            print(f"    UWAGA: Nie PDF — prawdopodobnie strona HTML ({len(resp.content)} bytes)")
            return None
        path.write_bytes(resp.content)
        print(f"    Zapisano: {filename} ({len(resp.content)} bytes)")
        return path
    except Exception as e:
        print(f"    BŁĄD pobierania: {e}")
        return None


def parse_vote_from_pdf(pdf_path: Path) -> list[dict]:
    """Parse a single vote PDF from BIP Wrocław.

    Each PDF is ONE vote with this structure (extracted text):
        74
        2. Głosowanie za wycofaniem - druk nr 1958/23
        LXXI Sesja Rady Miejskiej Wrocławia
        Głosowanie
        1
        Typ głosowania
        jawne
        Data głosowania:  13.07.2023 10:12
        Liczba uprawnionych
        37
        Głosy za
        21
        Liczba obecnych
        33
        Głosy przeciw
        11
        Liczba nieobecnych
        4
        Głosy wstrzymujące się
        1
        Obecni niegłosujący
        0
        ...
        Lp  Nazwisko i imię  Głos  Lp.  Nazwisko i imię  Głos
        1.  Bohdan Aniszczyk  ZA  20.  Mirosław Lach  ZA
        ...

    Returns list with 0 or 1 vote dicts.
    """
    votes = []
    try:
        doc = fitz.open(str(pdf_path))
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"    BŁĄD parsowania PDF: {e}")
        return votes

    lines = full_text.split('\n')

    # Check if this is a vote PDF — look for "Głosy za" or "Uprawnieni do głosowania"
    if not any("Głosy za" in l or "Uprawnieni do głosowania" in l for l in lines):
        return votes

    # --- Extract topic ---
    # Topic is typically the 2nd non-empty line (after a page/vote number)
    # Pattern: "N. Głosowanie w sprawie ..." or "N. Głosowanie za ..."
    topic = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip pure numbers (page numbers, vote numbers)
        if re.match(r'^\d+$', line):
            continue
        # Match topic line: starts with digit + dot + space
        if re.match(r'^\d+\.\s+', line):
            topic = re.sub(r'^\d+\.\s+', '', line).strip()
            break
        # Or just take the first meaningful line
        if len(line) > 10 and "Sesja" not in line and "Głosowanie" != line:
            topic = line
            break

    if not topic:
        topic = "Głosowanie"

    # --- Extract vote counts from header ---
    counts = {
        "za": 0,
        "przeciw": 0,
        "wstrzymal_sie": 0,
        "brak_glosu": 0,
        "nieobecni": 0,
    }

    for i, line in enumerate(lines):
        line_s = line.strip()
        if line_s == "Głosy za" and i + 1 < len(lines):
            try:
                counts["za"] = int(lines[i + 1].strip())
            except ValueError:
                pass
        elif line_s == "Głosy przeciw" and i + 1 < len(lines):
            try:
                counts["przeciw"] = int(lines[i + 1].strip())
            except ValueError:
                pass
        elif line_s.startswith("Głosy wstrzymujące") and i + 1 < len(lines):
            try:
                counts["wstrzymal_sie"] = int(lines[i + 1].strip())
            except ValueError:
                pass
        elif line_s == "Liczba nieobecnych" and i + 1 < len(lines):
            try:
                counts["nieobecni"] = int(lines[i + 1].strip())
            except ValueError:
                pass
        elif line_s == "Obecni niegłosujący" and i + 1 < len(lines):
            try:
                counts["brak_glosu"] = int(lines[i + 1].strip())
            except ValueError:
                pass

    # --- Extract individual votes from table ---
    # The table has lines like:
    #   "1."
    #   "Bohdan Aniszczyk"
    #   "ZA"
    #   "20."
    #   "Mirosław Lach"
    #   "ZA"
    # Lines come in triplets: number, name, vote
    # But sometimes two columns are interleaved.

    named_votes = {
        "za": [],
        "przeciw": [],
        "wstrzymal_sie": [],
        "brak_glosu": [],
        "nieobecni": [],
    }

    # Find the start of the vote table: after "Uprawnieni do głosowania" + header row
    table_start = None
    for i, line in enumerate(lines):
        if "Uprawnieni do głosowania" in line:
            table_start = i + 1
            break

    if table_start is None:
        # Try alternative: after "Nazwisko i imię" header
        for i, line in enumerate(lines):
            if "Nazwisko i imię" in line:
                table_start = i + 1
                break

    if table_start is None:
        return votes

    # Skip header lines (Lp, Nazwisko i imię, Głos)
    # These repeat: "Lp", "Nazwisko i imię", "Głos", "Lp.", "Nazwisko i imię", "Głos"
    while table_start < len(lines):
        l = lines[table_start].strip()
        if l in ("Lp", "Lp.", "Nazwisko i imię", "Głos", ""):
            table_start += 1
        else:
            break

    # Parse entries: each entry is (number, name, vote) — but lines may be interleaved
    # Strategy: collect all lines after table_start, skip numbers (N.), collect name+vote pairs
    table_lines = []
    for line in lines[table_start:]:
        l = line.strip()
        if not l:
            continue
        if l.startswith("Wydrukowano:"):
            break
        table_lines.append(l)

    # Parse: number → name → vote
    vote_values = {"ZA", "PRZECIW", "WSTRZYMUJĘ SIĘ", "NIEOBECNY", "NIEOBECNA",
                   "NIE GŁOSOWAŁ", "NIE GŁOSOWAŁA", "WSTRZYMAŁ SIĘ", "WSTRZYMAŁA SIĘ",
                   "OBECNY", "OBECNA"}

    i = 0
    pending_name = None
    while i < len(table_lines):
        item = table_lines[i]

        # Skip ordinal numbers (1., 2., etc.)
        if re.match(r'^\d+\.$', item):
            i += 1
            continue

        # Check if this is a vote value
        item_upper = item.upper().strip()
        if item_upper in vote_values:
            if pending_name:
                # Classify vote
                name = pending_name.replace("- ", "-").strip()
                # Fix names split with "- " (like "Gwadera- Urlep" → "Gwadera-Urlep")
                name = re.sub(r'-\s+', '-', name)

                if item_upper == "ZA":
                    named_votes["za"].append(name)
                elif item_upper == "PRZECIW":
                    named_votes["przeciw"].append(name)
                elif "WSTRZYMUJ" in item_upper or "WSTRZYMAŁ" in item_upper:
                    named_votes["wstrzymal_sie"].append(name)
                elif "NIE GŁOSOWAŁ" in item_upper:
                    named_votes["brak_glosu"].append(name)
                elif "NIEOBECN" in item_upper:
                    named_votes["nieobecni"].append(name)
                elif item_upper in ("OBECNY", "OBECNA"):
                    # Obecny/Obecna = present but did not vote
                    named_votes["brak_glosu"].append(name)

                pending_name = None
            i += 1
            continue

        # Otherwise it's a name (or continuation of multi-line name)
        # Check if next line is a vote value
        if pending_name:
            # This shouldn't happen unless name spans multiple lines
            # Try combining: "Razik-" + "Trziszka" etc.
            pending_name = pending_name + " " + item
        else:
            pending_name = item
        i += 1

    # Deduplicate
    for cat in named_votes:
        named_votes[cat] = list(dict.fromkeys(named_votes[cat]))

    total_named = sum(len(v) for v in named_votes.values())
    if total_named > 0:
        votes.append({
            "topic": topic[:500],
            "counts": counts,
            "named_votes": named_votes,
        })

    return votes


# ---------------------------------------------------------------------------
# Step 4: Build output structures
# ---------------------------------------------------------------------------

def load_profiles(profiles_path: str) -> dict:
    """Load profiles.json with councilor → club mapping."""
    path = Path(profiles_path)
    if not path.exists():
        print(f"  UWAGA: Brak {profiles_path} — kluby będą oznaczone jako '?'")
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = {}
    for p in data.get("profiles", []):
        name = p["name"]
        kadencje = p.get("kadencje", {})
        if kadencje:
            latest = list(kadencje.values())[-1]
            result[name] = {
                "name": name,
                "club": latest.get("club", "?"),
                "district": latest.get("okręg"),
            }
    return result


def compute_club_majority(vote: dict, profiles: dict) -> dict[str, str]:
    """For each club, compute the majority position in a given vote."""
    club_votes = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0})
    for cat in ["za", "przeciw", "wstrzymal_sie"]:
        for name in vote["named_votes"].get(cat, []):
            club = profiles.get(name, {}).get("club", "?")
            if club_has_line(club):
                club_votes[club][cat] += 1

    majority = {}
    for club, counts in club_votes.items():
        best = max(counts, key=counts.get)
        majority[club] = best
    return majority


def build_councilors(all_votes: list[dict], sessions: list[dict], profiles: dict) -> list[dict]:
    """Build councilor statistics from vote data."""
    all_names = set()
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    councilors = {}
    for name in sorted(all_names):
        prof = profiles.get(name, {})
        councilors[name] = {
            "name": name,
            "club": prof.get("club", "?"),
            "district": prof.get("district"),
            "votes_za": 0,
            "votes_przeciw": 0,
            "votes_wstrzymal": 0,
            "votes_brak": 0,
            "votes_nieobecny": 0,
            "sessions_present": set(),
            "votes_with_club": 0,
            "votes_against_club": 0,
            "rebellions": [],
        }

    for v in all_votes:
        club_majority = compute_club_majority(v, profiles)

        for name in v["named_votes"].get("za", []):
            if name in councilors:
                councilors[name]["votes_za"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "za", club_majority, v)
        for name in v["named_votes"].get("przeciw", []):
            if name in councilors:
                councilors[name]["votes_przeciw"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "przeciw", club_majority, v)
        for name in v["named_votes"].get("wstrzymal_sie", []):
            if name in councilors:
                councilors[name]["votes_wstrzymal"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
                _check_rebellion(councilors[name], "wstrzymal_sie", club_majority, v)
        for name in v["named_votes"].get("brak_glosu", []):
            if name in councilors:
                councilors[name]["votes_brak"] += 1
                councilors[name]["sessions_present"].add(v["session_date"])
        for name in v["named_votes"].get("nieobecni", []):
            if name in councilors:
                councilors[name]["votes_nieobecny"] += 1

    # Only count sessions that have vote data
    sessions_with_votes = set(v["session_date"] for v in all_votes if v.get("session_date"))
    total_sessions = len(sessions_with_votes)
    total_votes = len(all_votes)

    result = []
    for c in councilors.values():
        present_votes = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        frekwencja = (len(c["sessions_present"]) / total_sessions * 100) if total_sessions > 0 else 0
        aktywnosc = (present_votes / total_votes * 100) if total_votes > 0 else 0
        total_club_votes = c["votes_with_club"] + c["votes_against_club"]
        zgodnosc = (c["votes_with_club"] / total_club_votes * 100) if total_club_votes > 0 else 0

        result.append({
            "name": c["name"],
            "club": c["club"],
            "district": c["district"],
            "frekwencja": round(frekwencja, 1),
            "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": round(zgodnosc, 1),
            "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes,
            "rebellion_count": len(c["rebellions"]),
            "rebellions": c["rebellions"],
            "has_activity_data": False,
            "activity": None,
        })

    return sorted(result, key=lambda x: x["name"])


def _check_rebellion(councilor: dict, vote_cat: str, club_majority: dict, vote: dict):
    """Check if councilor voted differently from their club majority."""
    club = councilor["club"]
    if club == "?" or club not in club_majority:
        return
    majority_cat = club_majority[club]
    if vote_cat == majority_cat:
        councilor["votes_with_club"] += 1
    else:
        councilor["votes_against_club"] += 1
        councilor["rebellions"].append({
            "vote_id": vote["id"],
            "session": vote["session_date"],
            "topic": vote["topic"][:120],
            "their_vote": vote_cat,
            "club_majority": majority_cat,
        })


def compute_similarity(all_votes: list[dict], councilors_list: list[dict]) -> tuple[list, list]:
    """Compute councilor pairs with highest/lowest voting similarity."""
    name_to_club = {c["name"]: c["club"] for c in councilors_list}
    vectors = defaultdict(dict)
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
            "a": a,
            "b": b,
            "club_a": name_to_club.get(a, "?"),
            "club_b": name_to_club.get(b, "?"),
            "score": score,
            "common_votes": len(common),
        })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    top = pairs[:20]
    bottom = pairs[-20:][::-1]
    return top, bottom


def build_sessions(sessions_raw: list[dict], all_votes: list[dict]) -> list[dict]:
    """Build session data with attendee info."""
    votes_by_key = defaultdict(list)
    for v in all_votes:
        key = (v["session_date"], v.get("session_number", ""))
        votes_by_key[key].append(v)

    votes_by_date = defaultdict(list)
    for v in all_votes:
        votes_by_date[v["session_date"]].append(v)

    date_counts = Counter(s["date"] for s in sessions_raw)

    result = []
    for s in sessions_raw:
        date = s["date"]
        number = s.get("number", "")

        if date_counts[date] > 1:
            session_votes = votes_by_key.get((date, number), [])
        else:
            session_votes = votes_by_date.get(date, [])

        attendees = set()
        for v in session_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                attendees.update(v["named_votes"].get(cat, []))

        result.append({
            "date": date,
            "number": number,
            "vote_count": len(session_votes),
            "attendee_count": len(attendees),
            "attendees": sorted(attendees),
            "speakers": [],
        })

    return sorted(result, key=lambda x: (x["date"], x["number"]))


def merge_stats_to_profiles(profiles_path: str, output: dict):
    """Merge voting stats from data.json councilors into profiles.json."""
    path = Path(profiles_path)
    if not path.exists():
        print("  Pominięto merge — brak profiles.json")
        return

    with open(path, encoding="utf-8") as f:
        profiles = json.load(f)

    stats = {}
    for kad in output["kadencje"]:
        kid = kad["id"]
        for c in kad["councilors"]:
            stats[(kid, c["name"])] = c

    updated = 0
    for p in profiles.get("profiles", []):
        if "kadencje" not in p:
            p["kadencje"] = {}

        for kid, entry in p["kadencje"].items():
            c = stats.get((kid, p["name"]))
            if not c:
                continue
            for key in ["frekwencja", "aktywnosc", "zgodnosc_z_klubem",
                        "votes_za", "votes_przeciw", "votes_wstrzymal",
                        "votes_brak", "votes_nieobecny", "votes_total",
                        "rebellion_count", "rebellions"]:
                if key in c:
                    entry[key] = c[key]
            if not entry.get("club") and c.get("club"):
                entry["club"] = c["club"]
            entry["has_voting_data"] = True
            entry["has_activity_data"] = c.get("has_activity_data", False)
            if c.get("activity"):
                entry["activity"] = c["activity"]
            elif "activity" in entry:
                del entry["activity"]
            updated += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"  Zaktualizowano profiles.json: {updated} wpisów")


# ---------------------------------------------------------------------------
# Offline mode — build data from cached PDFs without network
# ---------------------------------------------------------------------------

def _extract_session_info_from_pdf(pdf_path: Path) -> dict | None:
    """Extract session number, date, and vote data from a cached PDF."""
    try:
        doc = fitz.open(str(pdf_path))
        text = doc[0].get_text()
        doc.close()
    except Exception:
        return None

    lines = text.split('\n')

    # Find session name: "XXVIII Sesja Rady Miejskiej Wrocławia"
    session_number = None
    for line in lines:
        m = re.search(r'([IVXLC]+)\s+Sesja\s+Rady\s+Miejskiej\s+Wroc', line)
        if m:
            session_number = m.group(1)
            break

    # Find date: "Data głosowania:  26.02.2026 14:19"
    session_date = None
    for line in lines:
        m = re.search(r'Data głosowania:\s+(\d{2})\.(\d{2})\.(\d{4})', line)
        if m:
            session_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            break

    if not session_number or not session_date:
        return None

    return {"number": session_number, "date": session_date}


def _run_offline(args):
    """Process cached PDFs without network access."""
    print("[OFFLINE] Tryb offline — parsowanie z cache PDFs\n")

    cache_dir = Path("pdfs")
    if not cache_dir.exists():
        print("BŁĄD: Brak katalogu pdfs/. Najpierw uruchom scraper online.")
        sys.exit(1)

    pdf_files = sorted(cache_dir.glob("protokol_*.pdf"))
    print(f"  Znaleziono {len(pdf_files)} PDFs w cache\n")

    if not pdf_files:
        print("BŁĄD: Brak plików PDF w cache.")
        sys.exit(1)

    # Parse all PDFs, group by session
    kadencja_start = datetime.strptime(KADENCJE["2024-2029"]["start"], "%Y-%m-%d")
    session_votes = defaultdict(list)  # session_key → votes
    session_meta = {}  # session_key → {number, date}
    skipped_old = 0
    parse_errors = 0

    for pi, pdf_path in enumerate(pdf_files):
        info = _extract_session_info_from_pdf(pdf_path)
        if not info:
            parse_errors += 1
            continue

        # Filter by kadencja
        vote_date = datetime.strptime(info["date"], "%Y-%m-%d")
        if not args.all_kadencje and vote_date < kadencja_start:
            skipped_old += 1
            continue

        session_key = f"{info['number']}_{info['date']}"
        session_meta[session_key] = info

        votes_from_pdf = parse_vote_from_pdf(pdf_path)
        if votes_from_pdf:
            vote_data = votes_from_pdf[0]
            vote_idx = len(session_votes[session_key])
            # Session number in vote_id prevents collisions when two
            # sessions share a date. Same bug pattern as Radom 2025-03-31.
            session_num = info.get("number") or ""
            num_part = f"_{session_num}" if session_num else ""
            vote_id = f"{info['date']}{num_part}_{vote_idx:03d}_000"
            vote = {
                "id": vote_id,
                "source_url": f"file://{pdf_path}",
                "source_page_url": info.get("url", ""),
                "session_date": info["date"],
                "session_number": info["number"],
                "topic": vote_data["topic"],
                "druk": None,
                "resolution": None,
                "counts": vote_data["counts"],
                "named_votes": vote_data["named_votes"],
            }
            session_votes[session_key].append(vote)

        if (pi + 1) % 50 == 0:
            print(f"  Sparsowano {pi + 1}/{len(pdf_files)} PDFs...")

    all_votes = []
    for key in sorted(session_votes.keys()):
        all_votes.extend(session_votes[key])

    all_sessions = []
    for key in sorted(session_meta.keys(), key=lambda k: session_meta[k]["date"]):
        meta = session_meta[key]
        all_sessions.append({
            "number": meta["number"],
            "date": meta["date"],
            "url": f"https://bip.um.wroc.pl/artykuly/1179/sesje-rady",
        })

    print(f"\n  Sparsowano: {len(all_votes)} głosowań z {len(all_sessions)} sesji")
    print(f"  Pominięto (stara kadencja): {skipped_old}, błędy parsowania: {parse_errors}")

    if not all_votes:
        print("BŁĄD: Nie znaleziono głosowań.")
        sys.exit(1)

    # Build output (same logic as online mode)
    print(f"\n[BUILD] Budowanie pliku wyjściowego...")

    profiles = load_profiles(args.profiles)
    if not profiles:
        profiles = {name: {"name": name, "club": club, "district": None}
                   for name, club in COUNCILORS.items()}
        print(f"  Załadowano profile: {len(profiles)} radnych (z listy hardcoded)")
    else:
        print(f"  Załadowano profile: {len(profiles)} radnych")

    kid = "2024-2029"
    councilors = build_councilors(all_votes, all_sessions, profiles)
    sessions_data = build_sessions(all_sessions, all_votes)
    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"  Kluby: {dict(club_counts)}")

    kad_output = {
        "id": kid,
        "label": KADENCJE[kid]["label"],
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

    output = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [kad_output],
    }

    out_path = Path(args.output)
    save_split_output(output, out_path)

    print(f"\nGotowe! Zapisano do {out_path}")
    total_v = len(all_votes)
    named_v = sum(1 for v in all_votes if sum(len(nv) for nv in v["named_votes"].values()) > 0)
    print(f"  {len(sessions_data)} sesji, {total_v} głosowań ({named_v} z imiennymi), {len(councilors)} radnych")

    merge_stats_to_profiles(args.profiles, output)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def compact_named_votes(output):
    """Convert named_votes from string arrays to indexed format for smaller JSON."""
    for kad in output.get("kadencje", []):
        names = set()
        for v in kad.get("votes", []):
            nv = v.get("named_votes", {})
            for cat_names in nv.values():
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
                nv[cat] = sorted(name_to_idx[n] for n in nv[cat] if isinstance(n, str) and n in name_to_idx)
    return output


def main():
    parser = argparse.ArgumentParser(description="Scraper Rady Miasta Wrocławia (BIP)")
    parser.add_argument("--output", default="docs/data.json", help="Plik wyjściowy")
    parser.add_argument("--delay", type=float, default=1.0, help="Opóźnienie między requestami (s)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Maks. sesji (0=wszystkie)")
    parser.add_argument("--max-pdfs", type=int, default=0, help="Maks. PDF na sesję (0=wszystkie, do testów)")
    parser.add_argument("--dry-run", action="store_true", help="Tylko lista sesji, bez głosowań")
    parser.add_argument("--profiles", default="docs/profiles.json", help="Plik profiles.json")
    parser.add_argument("--explore", action="store_true", help="Pobierz 1 sesję i pokaż strukturę")
    parser.add_argument("--all-kadencje", action="store_true", help="Nie filtruj po kadencji")
    parser.add_argument("--offline", action="store_true",
                        help="Tryb offline: parsuj tylko z cache PDFs (bez sieci)")
    parser.add_argument("--cache-dir", default=None,
                        help="Katalog cache HTML stron BIP. Domyślnie {script_dir}/../.cache/html. "
                             "Stabilne sesje (>7 dni temu) używają cache zamiast HTTP. "
                             "Ustaw '' aby wyłączyć cache.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Wyłącz cache HTML (każdy run = świeże HTTP dla wszystkich sesji).")
    args = parser.parse_args()

    global DELAY
    DELAY = args.delay

    print("=== Radoskop Scraper: Rada Miasta Wrocławia (BIP) ===")
    print(f"Backend: requests + BeautifulSoup + PyMuPDF")
    print()

    if args.offline:
        _run_offline(args)
        return

    init_session()

    # Cache HTML — default poza repo (cities/wroclaw/.cache/html/, w gitignore)
    if args.no_cache:
        cache_dir = None
    elif args.cache_dir is not None:
        cache_dir = args.cache_dir or None
    else:
        cache_dir = str(Path(__file__).resolve().parent.parent / ".cache" / "html")
    init_cache(cache_dir)
    if cache_dir:
        print(f"Cache HTML: {cache_dir}\n")

    total_steps = 3

    # 1. Session list
    print(f"[1/{total_steps}] Pobieranie listy sesji...")
    all_sessions = scrape_session_list()

    # Optionally include all kadencje
    if args.all_kadencje:
        print(f"  --all-kadencje: nie filtruję po dacie kadencji")

    if not all_sessions:
        print("BŁĄD: Nie znaleziono sesji.")
        print(f"Sprawdź ręcznie: {SESSIONS_URLS[0]}")
        sys.exit(1)

    if args.max_sessions > 0:
        all_sessions = all_sessions[:args.max_sessions]
        print(f"  (ograniczono do {args.max_sessions} sesji)")

    if args.dry_run:
        print("\nZnalezione sesje:")
        for s in all_sessions:
            print(f"  {s['number']:>8} | {s['date']} | {s['url']}")
        return

    if args.explore:
        s0 = all_sessions[-1]  # latest session
        print(f"\n[explore] Sesja {s0['number']} ({s0['date']})")
        print(f"  URL: {s0['url']}")
        soup = fetch(s0["url"])

        # Show all attachment links grouped by type
        all_att = []
        vote_att = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "/attachments/download/" in href:
                all_att.append((text, href))
                text_lower = text.lower()
                if "wynik głosowania" in text_lower or "wyniki głosowania" in text_lower:
                    vote_att.append((text, href))

        print(f"\n--- Załączniki: {len(all_att)} ogółem, {len(vote_att)} wyników głosowań ---")
        for text, href in vote_att[:5]:
            print(f"  [VOTE] {text}")
            print(f"    -> {href}")
        if len(vote_att) > 5:
            print(f"  ... i {len(vote_att)-5} więcej wyników głosowań")

        # Try downloading and parsing first vote PDF
        target = vote_att if vote_att else all_att
        if target:
            cache = Path("pdfs")
            cache.mkdir(exist_ok=True)
            text, url = target[0]
            if not url.startswith("http"):
                url = urljoin(BIP_BASE, url)
            print(f"\n--- Próba: {text} ---")
            pdf_path = download_pdf(url, cache)
            if pdf_path:
                result = parse_vote_from_pdf(pdf_path)
                if result:
                    v = result[0]
                    total = sum(len(nv) for nv in v["named_votes"].values())
                    print(f"  Temat: {v['topic'][:80]}")
                    print(f"  Głosy: za={v['counts']['za']}, przeciw={v['counts']['przeciw']}, wstrz={v['counts']['wstrzymal_sie']}")
                    print(f"  Imiennych: {total}")
                    print(f"  Przykład ZA: {v['named_votes']['za'][:3]}")
                else:
                    print(f"  Nie sparsowano głosowania z PDF")
                    import fitz as _fitz
                    d = _fitz.open(str(pdf_path))
                    txt = d[0].get_text()[:500]
                    d.close()
                    print(f"  Tekst PDF (500 znaków):\n{txt}")
        return

    # 2. Fetch PDFs and parse votes for each session
    print(f"\n[2/{total_steps}] Pobieranie protokołów i głosowań ({len(all_sessions)} sesji)...")
    all_votes = []
    cache_dir = Path("pdfs")
    cache_dir.mkdir(parents=True, exist_ok=True)

    for si, session in enumerate(all_sessions):
        print(f"\n  Sesja {session['number']} ({session['date']}) [{si+1}/{len(all_sessions)}]")

        pdf_links = scrape_session_pdf_links(session)

        if args.max_pdfs > 0:
            pdf_links = pdf_links[:args.max_pdfs]
            print(f"    (ograniczono do {args.max_pdfs} PDF)")

        for pi, pdf_link in enumerate(pdf_links):
            pdf_path = download_pdf(pdf_link["url"], cache_dir)
            if not pdf_path:
                continue

            votes_from_pdf = parse_vote_from_pdf(pdf_path)
            print(f"    Sparsowano {len(votes_from_pdf)} głosowań z PDF")

            for vi, vote_data in enumerate(votes_from_pdf):
                vote_id = f"{session['date']}_{pi:03d}_{vi:03d}"
                vote = {
                    "id": vote_id,
                    "source_url": pdf_link["url"],
                    "source_page_url": session.get("url", ""),
                    "session_date": session["date"],
                    "session_number": session["number"],
                    "topic": vote_data["topic"],
                    "druk": pdf_link.get("druk"),
                    "resolution": None,
                    "counts": vote_data["counts"],
                    "named_votes": vote_data["named_votes"],
                }
                all_votes.append(vote)

    print(f"\n  Razem: {len(all_votes)} głosowań z {len(all_sessions)} sesji")

    if not all_votes:
        print("UWAGA: Nie znaleziono głosowań.")
        sys.exit(1)

    # 3. Build output
    print(f"\n[3/{total_steps}] Budowanie pliku wyjściowego...")

    # Load or use hardcoded profiles
    profiles = load_profiles(args.profiles)
    if not profiles:
        # Use hardcoded COUNCILORS if no profiles.json
        profiles = {name: {"name": name, "club": club, "district": None}
                   for name, club in COUNCILORS.items()}
        print(f"  Załadowano profile: {len(profiles)} radnych (z listy hardcoded)")
    else:
        print(f"  Załadowano profile: {len(profiles)} radnych")

    kid = "2024-2029"
    councilors = build_councilors(all_votes, all_sessions, profiles)
    sessions_data = build_sessions(all_sessions, all_votes)
    sim_top, sim_bottom = compute_similarity(all_votes, councilors)

    club_counts = defaultdict(int)
    for c in councilors:
        club_counts[c["club"]] += 1

    print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
    print(f"  Kluby: {dict(club_counts)}")

    kad_output = {
        "id": kid,
        "label": KADENCJE[kid]["label"],
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

    output = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kid,
        "kadencje": [kad_output],
    }

    out_path = Path(args.output)
    save_split_output(output, out_path)

    print(f"\nGotowe! Zapisano do {out_path}")
    total_v = len(all_votes)
    named_v = sum(1 for v in all_votes if sum(len(nv) for nv in v["named_votes"].values()) > 0)
    print(f"  {len(sessions_data)} sesji, {total_v} głosowań ({named_v} z imiennymi), {len(councilors)} radnych")

    # Merge stats into profiles.json
    merge_stats_to_profiles(args.profiles, output)



def save_split_output(output, out_path):
    """Save output as split files: data.json (index) + kadencja-{id}.json per kadencja."""
    import json as _json
    from pathlib import Path as _Path
    compact_named_votes(output)
    out_path = _Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        kad_path = out_path.parent / f"kadencja-{kid}.json"
        with open(kad_path, "w", encoding="utf-8") as f:
            _json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "generated": output.get("generated", ""),
        "default_kadencja": output.get("default_kadencja", ""),
        "kadencje": stubs,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
