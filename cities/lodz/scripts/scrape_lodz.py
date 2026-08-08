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


def fetch(url: str) -> BeautifulSoup:
    """Fetch a page and return BeautifulSoup."""
    time.sleep(DELAY)
    print(f"  GET {url}")
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
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

        # Match text like "XXVII sesja Rady Miejskiej"
        # Also extract date from PDF filename or context
        m = re.search(
            r'([IVXLCDM]+)\s+sesja|sesja\s+nr\s+([IVXLCDM]+)',
            text,
            re.IGNORECASE
        )
        if not m:
            continue

        session_num = m.group(1) or m.group(2)
        if not session_num:
            continue

        session_num = session_num.upper()

        if not href.startswith("http"):
            href = urljoin(base_url, href)

        # Try to extract date from text first ("25 lutego 2026 r.")
        date = None
        date_from_text = re.search(
            r'(\d{1,2})\s+(\w+)\s+(\d{4})', text
        )
        if date_from_text:
            date = parse_polish_date(
                f"{date_from_text.group(1)} {date_from_text.group(2)} {date_from_text.group(3)}"
            )

        # Fallback: extract date from filename (YYYYMMDD pattern)
        if not date:
            date_match = re.search(r'(\d{8})', href)
            if date_match:
                datestr = date_match.group(1)
                # YYYYMMDD format
                date = f"{datestr[0:4]}-{datestr[4:6]}-{datestr[6:8]}"

        if session_num and (href or date):
            sessions.append({
                "number": session_num,
                "date": date,
                "url": href,
            })

    return sessions


def _fetch_paginated(base_url: str) -> list[dict]:
    """Fetch a BIP listing page + all pagination pages, extract sessions."""
    sessions = []
    visited = set()

    try:
        soup = fetch(base_url)
    except Exception as e:
        print(f"  Nie udało się pobrać {base_url}: {e}")
        return sessions

    sessions.extend(_extract_sessions_from_soup(soup, base_url))
    visited.add(base_url)

    # Follow pagination links
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        # Check for numbered pagination links
        if re.match(r'^\d+$', text) and int(text) > 1:
            page_url = urljoin(base_url, href)
            if page_url not in visited:
                visited.add(page_url)
                try:
                    page_soup = fetch(page_url)
                    sessions.extend(_extract_sessions_from_soup(page_soup, base_url))
                except Exception:
                    pass
        # Check for "następna" / ">" links
        elif text.lower() in ("następna", "»", ">", "next"):
            page_url = urljoin(base_url, href)
            if page_url not in visited:
                visited.add(page_url)
                try:
                    page_soup = fetch(page_url)
                    sessions.extend(_extract_sessions_from_soup(page_soup, base_url))
                except Exception:
                    pass

    return sessions


def scrape_session_list() -> list[dict]:
    """Fetch session list from BIP Łódź."""
    sessions = []

    for url in SESSIONS_URLS:
        print(f"  Próbuję: {url}")
        try:
            page_sessions = _fetch_paginated(url)
            if page_sessions:
                print(f"    → znaleziono {len(page_sessions)} sesji")
                sessions.extend(page_sessions)
        except Exception as e:
            print(f"    → błąd: {e}")

    if not sessions:
        print("  UWAGA: Nie znaleziono sesji na żadnej stronie!")
        return []

    # Deduplicate by (number, date)
    seen = set()
    unique = []
    for s in sessions:
        key = (s["number"], s["date"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    # Filter by kadencja — only sessions from 2024-05-07 onwards
    kadencja_start = KADENCJE["2024-2029"]["start"]
    filtered = [s for s in unique if s.get("date") and s["date"] >= kadencja_start]

    # If no filtered sessions, include all (with warning)
    if not filtered:
        if unique:
            dates = [s.get("date") or "" for s in unique]
            newest = max(d for d in dates if d) if any(dates) else "brak dat"
            print(f"  UWAGA: Brak sesji po {kadencja_start}.")
            print(f"  Najnowsza znaleziona: {newest}")
        filtered = sorted(unique, key=lambda x: x.get("date") or "")

    print(f"  Znaleziono {len(unique)} sesji ogółem, {len(filtered)} w kadencji 2024-2029")

    return sorted(filtered, key=lambda x: x.get("date") or "")


# ---------------------------------------------------------------------------
# Step 2: Extract PDF links from session page
# ---------------------------------------------------------------------------

def scrape_session_pdf_links(session: dict) -> list[dict]:
    """Get PDF links for a session.

    BIP Łódź links directly to PDF files (one PDF per session containing
    all votes from that session). If session URL is already a PDF, return
    it directly. Otherwise try fetching as HTML to find PDF links.
    """
    url = session.get("url", "")
    if not url:
        return []

    # BIP Łódź: URL is already a PDF
    if url.lower().endswith(".pdf"):
        return [{
            "url": url,
            "text": f"Sesja {session.get('number', '?')}",
        }]

    # Fallback: fetch HTML page and look for PDF links
    try:
        soup = fetch(url)
    except Exception as e:
        print(f"    BŁĄD pobierania {url}: {e}")
        return []

    vote_links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        # Look for PDF links (wyniki głosowań)
        if ".pdf" not in href.lower():
            continue

        # Check if it's about voting results
        text_lower = text.lower()
        is_vote = (
            "wynik" in text_lower or "głosow" in text_lower
            or "brm" in text_lower  # Filename pattern
        )

        if not is_vote:
            continue

        if not href.startswith("http"):
            href = urljoin(BIP_BASE, href)

        vote_links.append({
            "url": href,
            "text": text,
        })

    return vote_links


# ---------------------------------------------------------------------------
# Step 3: Parse PDF — extract votes
# ---------------------------------------------------------------------------

def _swap_name(name: str) -> str:
    """Swap 'Nazwisko Imię' → 'Imię Nazwisko'."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return " ".join(parts[1:]) + " " + parts[0]
    return name.strip()


def download_pdf(pdf_url: str, cache_dir: Path) -> Path | None:
    """Download a PDF from URL to cache directory. Skip if already cached."""
    # Build filename from URL
    filename = pdf_url.split("/")[-1]
    if not filename.endswith(".pdf"):
        # Fallback: hash the URL
        import hashlib
        filename = hashlib.md5(pdf_url.encode()).hexdigest() + ".pdf"

    path = cache_dir / filename

    if path.exists() and path.stat().st_size > 1000:
        print(f"      Cache hit: {filename}")
        return path

    time.sleep(DELAY)
    try:
        resp = _session.get(pdf_url, timeout=60)
        resp.raise_for_status()
        if b"%PDF" not in resp.content[:10]:
            print(f"      UWAGA: Nie PDF — prawdopodobnie strona HTML "
                  f"({len(resp.content)} bytes)")
            return None
        path.write_bytes(resp.content)
        print(f"      Zapisano: {filename} ({len(resp.content)} bytes)")
        return path
    except Exception as e:
        print(f"      BŁĄD pobierania PDF {pdf_url}: {e}")
        return None


def extract_votes_from_pdf(pdf_path: Path, debug: bool = False) -> list[dict]:
    """Parse a cached voting results PDF using PyMuPDF find_tables().

    BIP Łódź PDFs have tables where each vote is an "X" placed in one of
    the columns (ZA / PRZECIW / WSTRZYMUJĄCY SIĘ). We use find_tables()
    to reliably extract structured table data with correct column mapping.

    Returns list of:
        {
            "subject": "...",
            "date": "2024-11-25",
            "votes": {"Imię Nazwisko": "ZA"|"PRZECIW"|"WSTRZYMAŁ SIĘ"|"NIEOBECNY", ...}
        }
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"      BŁĄD otwierania PDF {pdf_path}: {e}")
        return []

    # --- Pass 1: plain text for debug + subject/date extraction ---
    full_text = ""
    try:
        for page in doc:
            full_text += page.get_text() + "\n"
    except Exception as e:
        doc.close()
        print(f"      BŁĄD parsowania PDF {pdf_url}: {e}")
        return []

    if debug:
        print(f"\n{'='*60}")
        print(f"DEBUG: Surowy tekst z PDF ({len(full_text)} znaków):")
        print(f"{'='*60}")
        print(full_text[:3000])
        print(f"{'='*60}\n")

    # --- Extract subject + date per vote block from plain text ---
    block_marker = "Wyniki głosowania jawnego imiennego"
    text_blocks = re.split(re.escape(block_marker), full_text)

    block_meta = []  # [(subject, date), ...]
    for bt in text_blocks[1:]:
        lines = [l.strip() for l in bt.split('\n')]
        subj_parts = []
        date_val = None
        for line in lines:
            if not line or "nad punktem" in line.lower():
                continue
            m = re.search(r'Wyniki zapisano dnia:\s*(\d{4})-(\d{2})-(\d{2})', line)
            if m:
                date_val = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                break
            m2 = re.search(r'Wyniki zapisano dnia:\s*(\d{2})\.(\d{2})\.(\d{4})', line)
            if m2:
                date_val = f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
                break
            m3 = re.search(r'zapisano dnia:\s*(\d{4}-\d{2}-\d{2})', line)
            if m3:
                date_val = m3.group(1)
                break
            subj_parts.append(line)
        subject = " ".join(subj_parts).strip()
        subject = re.sub(r'^\d+\.\s*', '', subject).strip() or "(brak tematu)"
        block_meta.append((subject, date_val))

    if debug:
        print(f"DEBUG: {len(block_meta)} bloków głosowań w tekście")

    # --- Pass 2: extract tables using find_tables() ---
    all_table_rows = []  # list of extracted tables (each = list of rows)
    for page_num, page in enumerate(doc):
        try:
            tables = page.find_tables()
            for table in tables:
                rows = table.extract()
                if rows and len(rows) >= 2:
                    all_table_rows.append(rows)
                    if debug:
                        print(f"  DEBUG strona {page_num+1}: tabela "
                              f"{len(rows)} wierszy, nagłówek: {rows[0]}")
        except Exception as e:
            if debug:
                print(f"  DEBUG strona {page_num+1}: find_tables error: {e}")
    doc.close()

    if debug:
        print(f"DEBUG: {len(all_table_rows)} tabel znalezionych")

    # --- Process tables into vote blocks ---
    all_results = []
    meta_idx = 0  # tracks which block_meta to use

    for table_data in all_table_rows:
        votes = _parse_vote_table(table_data, debug=debug)
        if not votes or len(votes) < 5:
            continue

        # Get metadata for this table
        if meta_idx < len(block_meta):
            subj, date_val = block_meta[meta_idx]
        else:
            subj, date_val = "(brak tematu)", None

        all_results.append({
            "subject": subj,
            "date": date_val,
            "votes": votes,
        })

        if debug:
            print(f"  [block {meta_idx+1}] OK: {len(votes)} głosów, "
                  f"temat: {subj[:60]}")

        meta_idx += 1

    # If find_tables found fewer tables than expected blocks, it might be
    # because multiple vote blocks share one large table spanning pages.
    # Handle this: if we got 0 results from tables but have block_meta,
    # try a fallback text-only approach.
    if not all_results and block_meta:
        if debug:
            print("DEBUG: find_tables() nie znalazło tabel, próbuję fallback")
        all_results = _fallback_text_parse(full_text, block_meta, debug)

    return all_results


def _parse_vote_table(rows: list[list], debug: bool = False) -> dict:
    """Parse a single extracted table into a votes dict.

    Expected format:
      Header row: ["Lp.", "Imię i nazwisko", "ZA", "PRZECIW", "WSTRZYMUJĄCY SIĘ"]
      Data rows:  ["1.", "Anielak Tomasz", "X", "", ""]
      or          ["1.", "Anielak Tomasz", "X", None, None]

    Returns dict: {"Imię Nazwisko": "ZA"|"PRZECIW"|..., ...}
    """
    if not rows:
        return {}

    # Find header row (contains column names like ZA, PRZECIW)
    header = None
    header_idx = 0
    for i, row in enumerate(rows):
        row_text = " ".join((c or "") for c in row).upper()
        if "ZA" in row_text and ("PRZECIW" in row_text or "WSTRZYM" in row_text):
            header = row
            header_idx = i
            break

    if header is None:
        return {}

    # Map column indices to vote types
    col_map = {}  # col_index -> vote_type
    name_col = None
    for ci, cell in enumerate(header):
        if cell is None:
            continue
        cu = cell.strip().upper()
        if cu == "ZA":
            col_map[ci] = "ZA"
        elif "PRZECIW" in cu:
            col_map[ci] = "PRZECIW"
        elif "WSTRZYM" in cu:
            col_map[ci] = "WSTRZYMAŁ SIĘ"
        elif "IMIĘ" in cu or "NAZWISKO" in cu:
            name_col = ci
        elif cu in ("LP.", "LP"):
            pass  # skip

    if name_col is None:
        name_col = 1  # default: second column

    if not col_map:
        return {}

    # Parse data rows
    votes = {}
    absent_mode = False

    for row in rows[header_idx + 1:]:
        if not row:
            continue

        row_text = " ".join((c or "") for c in row).strip()
        row_upper = row_text.upper()

        # Detect absent section
        if "OSOBY NIEOBECNE" in row_upper:
            absent_mode = True
            continue
        if "OSOBY OBECNE" in row_upper:
            continue

        # Get name
        name = (row[name_col] if name_col < len(row) else None) or ""
        name = name.strip()
        if not name or len(name) < 3:
            continue

        # Skip header-like rows
        name_upper = name.upper()
        if "IMIĘ" in name_upper or "NAZWISKO" in name_upper:
            continue
        if name_upper in ("LP.", "LP"):
            continue

        if absent_mode:
            votes[_swap_name(name)] = "NIEOBECNY"
            continue

        # Find which column has "X"
        vote_type = "BRAK GŁOSU"
        for ci, vtype in col_map.items():
            cell_val = (row[ci] if ci < len(row) else None) or ""
            if cell_val.strip().upper() == "X":
                vote_type = vtype
                break

        votes[_swap_name(name)] = vote_type

    return votes


def _fallback_text_parse(full_text: str, block_meta: list,
                         debug: bool = False) -> list[dict]:
    """Fallback: parse vote blocks from plain text when find_tables fails.

    This is a simpler approach that works when all votes in a block are
    the same type (can't distinguish columns). Used as last resort.
    """
    block_marker = "Wyniki głosowania jawnego imiennego"
    blocks = re.split(re.escape(block_marker), full_text)
    all_results = []

    for bi, block_text in enumerate(blocks[1:]):
        lines = [l.strip() for l in block_text.split('\n')]
        votes = {}
        in_names = False

        for i, line in enumerate(lines):
            if not line:
                continue
            # Detect start of name list (after column headers)
            if re.match(r'^\d+\.\s*$', line) and not in_names:
                in_names = True
            if not in_names:
                continue

            # Row number line
            if re.match(r'^\d+\.\s*$', line):
                # Next non-empty line should be the name
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].strip() and not re.match(r'^\d+\.\s*$', lines[j]):
                        name = lines[j].strip()
                        if len(name) > 2:
                            votes[_swap_name(name)] = "ZA"  # can't determine column
                        break

        if bi < len(block_meta):
            subj, dt = block_meta[bi]
        else:
            subj, dt = "(brak tematu)", None

        if votes and len(votes) >= 5:
            all_results.append({
                "subject": subj,
                "date": dt,
                "votes": votes,
            })

    return all_results


# ---------------------------------------------------------------------------
# Step 4: Build voting data structure
# ---------------------------------------------------------------------------

def parse_vote_results(all_votes: list[dict]) -> list[dict]:
    """Convert vote data to Wrocław-compatible format with named_votes + counts."""
    result = []

    for v in all_votes:
        vote_id = v.get("vote_id", "")
        session_number = v.get("session_number", "")
        session_date = v.get("session_date", "")
        subject = v.get("subject", "")
        votes = v.get("votes", {})

        if not subject or not votes:
            continue

        named_votes = {
            "za": [],
            "przeciw": [],
            "wstrzymal_sie": [],
            "brak_glosu": [],
            "nieobecni": [],
        }

        for name, vote in votes.items():
            if vote == "ZA":
                named_votes["za"].append(name)
            elif vote == "PRZECIW":
                named_votes["przeciw"].append(name)
            elif vote == "WSTRZYMAŁ SIĘ":
                named_votes["wstrzymal_sie"].append(name)
            elif vote == "NIEOBECNY":
                named_votes["nieobecni"].append(name)
            else:  # BRAK GŁOSU, etc.
                named_votes["brak_glosu"].append(name)

        counts = {
            "za": len(named_votes["za"]),
            "przeciw": len(named_votes["przeciw"]),
            "wstrzymal_sie": len(named_votes["wstrzymal_sie"]),
            "brak_glosu": len(named_votes["brak_glosu"]),
            "nieobecni": len(named_votes["nieobecni"]),
        }

        # Wyciąg numeru druku z tekstu tematu. BIP Łódź wkleja string typu
        # "druk nr 87/2026" albo "Druk Nr 87/2026" w opisie głosowania.
        # Niektóre tematy mają sekwencję wielu druków (autopoprawki); biorę
        # pierwszy. Format: liczba/rok.
        druk_match = re.search(r"druk\s*nr\s*([\w\-/]+)", subject, re.IGNORECASE)
        druk = druk_match.group(1).strip() if druk_match else None

        result.append({
            "id": vote_id,
            "source_url": v.get("source_url", ""),
            "session_date": session_date,
            "session_number": session_number,
            "topic": subject,
            "druk": druk,
            "resolution": None,
            "counts": counts,
            "named_votes": named_votes,
        })

    return result


def build_councilor_stats(all_votes: list[dict]) -> dict:
    """Build councilor voting statistics."""
    stats = {}
    CATS = ["za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"]

    for v in all_votes:
        for cat in CATS:
            for name in v["named_votes"].get(cat, []):
                if name not in stats:
                    stats[name] = {c: 0 for c in CATS}
                stats[name][cat] += 1

    return stats


MANDATE_END = {
    "Włodzimierz Tomaszewski": "2024-06-21",
}


def make_slug(name: str) -> str:
    """Create URL-safe slug from Polish name."""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    slug = name.lower()
    for pl, ascii_c in replacements.items():
        slug = slug.replace(pl, ascii_c)
    slug = slug.replace(' ', '-').replace("'", "")
    return slug


def build_councilor_profiles(all_votes: list[dict], kadencja_id: str = "2024-2029") -> list[dict]:
    """Build councilor profile entries in kadencje format (matching template)."""
    stats = build_councilor_stats(all_votes)
    name_to_club = COUNCILORS

    profiles = []
    for name in sorted(stats.keys()):
        s = stats[name]
        voted = s["za"] + s["przeciw"] + s["wstrzymal_sie"] + s["brak_glosu"]
        total = voted + s["nieobecni"]
        if total == 0:
            continue

        frekwencja = round(voted / total * 100, 1) if total else 0
        active = s["za"] + s["przeciw"] + s["wstrzymal_sie"]
        aktywnosc = round(active / voted * 100, 1) if voted > 0 else 0.0
        club = name_to_club.get(name, "?")

        entry = {
            "club": club,
            "frekwencja": frekwencja,
            "aktywnosc": aktywnosc,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": s["za"],
            "votes_przeciw": s["przeciw"],
            "votes_wstrzymal": s["wstrzymal_sie"],
            "votes_brak": s["brak_glosu"],
            "votes_nieobecny": s["nieobecni"],
            "votes_total": total,
            "rebellion_count": 0,
            "rebellions": [],
            "has_voting_data": True,
            "has_activity_data": False,
            "roles": [],
            "notes": "",
            "former": False,
            "mid_term": False,
        }

        if name in MANDATE_END:
            entry["mandate_end"] = MANDATE_END[name]
            entry["former"] = True
            entry["notes"] = f"Mandat wygaszony {MANDATE_END[name]}"

        profiles.append({
            "name": name,
            "slug": make_slug(name),
            "kadencje": {
                kadencja_id: entry,
            },
        })

    return profiles


def compute_club_agreement(all_votes: list[dict], name_to_club: dict) -> dict:
    """Compute agreement scores with own party."""
    agreement = {}

    for v in all_votes:
        # Only count people who actually voted (za/przeciw/wstrzymal)
        voters = {}
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                voters[name] = cat

        for name, vote_cat in voters.items():
            club = name_to_club.get(name)
            if not club_has_line(club):
                continue

            if name not in agreement:
                agreement[name] = {"agree": 0, "total": 0}
            agreement[name]["total"] += 1

            # Find club majority vote on this item
            club_votes = {}
            for cat in ["za", "przeciw", "wstrzymal_sie"]:
                for n in v["named_votes"].get(cat, []):
                    if n != name and name_to_club.get(n) == club:
                        club_votes[cat] = club_votes.get(cat, 0) + 1

            if club_votes:
                majority_cat = max(club_votes, key=club_votes.get)
                if majority_cat == vote_cat:
                    agreement[name]["agree"] += 1

    result = {}
    for name, scores in agreement.items():
        if scores["total"] > 0:
            result[name] = round((scores["agree"] / scores["total"]) * 100, 1)
        else:
            result[name] = 0.0

    return result


def compute_rebellions(all_votes: list[dict], name_to_club: dict) -> dict:
    """Find votes where a councillor voted against their club majority."""
    rebellions = {}  # name -> list of rebellion dicts

    for v in all_votes:
        voters = {}
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                voters[name] = cat

        for name, vote_cat in voters.items():
            club = name_to_club.get(name)
            if not club_has_line(club):
                continue

            club_votes = {}
            for cat in ["za", "przeciw", "wstrzymal_sie"]:
                for n in v["named_votes"].get(cat, []):
                    if n != name and name_to_club.get(n) == club:
                        club_votes[cat] = club_votes.get(cat, 0) + 1

            if club_votes:
                majority_cat = max(club_votes, key=club_votes.get)
                if majority_cat != vote_cat:
                    if name not in rebellions:
                        rebellions[name] = []
                    rebellions[name].append({
                        "vote_id": v["id"],
                        "session": v.get("session_date", ""),
                        "topic": v.get("topic", ""),
                        "their_vote": vote_cat,
                        "club_majority": majority_cat,
                    })

    return rebellions


def compute_similarity(all_votes: list[dict], name_to_club: dict,
                       top_n: int = 10) -> tuple[list, list]:
    """Compute pairwise voting similarity between councillors."""
    from itertools import combinations

    # Build per-vote mapping: name → vote category
    councilor_names = set()
    for v in all_votes:
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                councilor_names.add(name)

    # Compute pairwise similarity
    pairs = {}
    common = {}
    for v in all_votes:
        voters = {}
        for cat in ["za", "przeciw", "wstrzymal_sie"]:
            for name in v["named_votes"].get(cat, []):
                voters[name] = cat
        voter_list = sorted(voters.keys())
        for a, b in combinations(voter_list, 2):
            key = (a, b) if a < b else (b, a)
            if key not in pairs:
                pairs[key] = 0
                common[key] = 0
            common[key] += 1
            if voters[a] == voters[b]:
                pairs[key] += 1

    scored = []
    for (a, b), agree in pairs.items():
        total = common[(a, b)]
        if total >= 10:
            score = round(agree / total * 100, 1)
            scored.append({
                "a": a, "b": b,
                "club_a": name_to_club.get(a, "?"),
                "club_b": name_to_club.get(b, "?"),
                "score": score,
                "common_votes": total,
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]
    bottom = list(reversed(scored[-top_n:])) if len(scored) >= top_n else list(reversed(scored))

    return top, bottom


def build_sessions_summary(all_votes: list[dict]) -> list[dict]:
    """Build per-session summary matching Wrocław sessions format."""
    sessions_map = {}

    for v in all_votes:
        sdate = v.get("session_date", "")
        snum = v.get("session_number", "")
        key = (sdate, snum)
        if key not in sessions_map:
            sessions_map[key] = {
                "date": sdate,
                "number": snum,
                "vote_count": 0,
                "attendees": set(),
            }
        sessions_map[key]["vote_count"] += 1
        for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
            for name in v["named_votes"].get(cat, []):
                sessions_map[key]["attendees"].add(name)

    result = []
    for key in sorted(sessions_map.keys()):
        s = sessions_map[key]
        attendees = sorted(s["attendees"])
        result.append({
            "date": s["date"],
            "number": s["number"],
            "vote_count": s["vote_count"],
            "attendee_count": len(attendees),
            "attendees": attendees,
            "speakers": [],
        })

    return result


def _extract_votes_cached(pdf_path: Path, parse_cache_dir: Path | None, debug: bool = False) -> list[dict]:
    """Wrapper na extract_votes_from_pdf z disk cache po sha256(pdf).

    1730 głosowań × pdfplumber parse to ~10-14min serial - cache eliminuje
    reparse dla niezmienionych PDF-ów (a PDF-y BIP nie zmieniają się po
    publikacji). Klucz: sha256(pdf bytes), wynik: JSON z list[dict].
    Cache w parse_cache_dir/{sha}.json.
    """
    if parse_cache_dir is None:
        return extract_votes_from_pdf(pdf_path, debug=debug)
    import hashlib as _h
    sha = _h.sha256(pdf_path.read_bytes()).hexdigest()
    cache_file = parse_cache_dir / f"{sha}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache, reparse
    blocks = extract_votes_from_pdf(pdf_path, debug=debug)
    parse_cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache_file.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"      [warn] nie zapisałem parse cache {sha[:8]}: {exc}")
    return blocks


def scrape(output_path: str, profiles_path: str, pdf_dir: str | None = None, debug: bool = False):
    """Main scraping function."""
    init_session()

    print("\n=== Pobieranie listy sesji ===")
    sessions = scrape_session_list()
    if not sessions:
        print("BŁĄD: Brak sesji do przetworzenia")
        return

    all_raw_votes = []
    # PDF cache: pliki PDF z BIP, persystentne między runami. Jeśli pdf_dir
    # podany przez pipeline (--pdf-dir scratch_dir/pdfs), używamy go;
    # inaczej fallback do "pdfs" obok cwd dla local dev.
    cache_dir = Path(pdf_dir) if pdf_dir else Path("pdfs")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Parse cache: parsed JSON-y per sha256(pdf), żeby reparse robił się
    # tylko dla nowych/zmienionych PDF-ów. Trzymane obok PDF-ów w scratch.
    parse_cache_dir = cache_dir.parent / "parsed_votes"

    print(f"\n=== Pobieranie PDF-ów ({len(sessions)} sesji) ===")
    for i, session in enumerate(sessions):
        session_number = session.get("number", "")
        session_date = session.get("date")
        session_url = session.get("url")
        # Counter resetowany per sesja, żeby vote_id był NN per sesja (001, 002,
        # ..., NNN), a nie cumulative przez całą kadencję. Bez tego XXIX
        # dostawała IDs 1646-1730 (kontynuacja z poprzednich sesji), co psuło
        # routing i deep link do konkretnego głosowania.
        vote_counter = 0

        print(f"\n  [{i+1}/{len(sessions)}] Sesja {session_number} ({session_date})")

        if not session_url:
            print(f"    Brak URL dla sesji {session_number}")
            continue

        pdf_links = scrape_session_pdf_links(session)
        print(f"    Znaleziono {len(pdf_links)} PDF-ów")

        for pdf_link in pdf_links:
            pdf_url = pdf_link["url"]
            print(f"    Pobieram: {pdf_url.split('/')[-1]}")

            pdf_path = download_pdf(pdf_url, cache_dir)
            if not pdf_path:
                print(f"      Brak pliku PDF")
                continue

            vote_blocks = _extract_votes_cached(pdf_path, parse_cache_dir, debug=debug)
            if not vote_blocks:
                print(f"      Brak głosów w PDF")
                continue

            print(f"      Znaleziono {len(vote_blocks)} głosowań w PDF")
            for vb in vote_blocks:
                vote_counter += 1
                all_raw_votes.append({
                    "vote_id": f"{session_date}_{vote_counter:03d}_000",
                    "session_number": session_number,
                    "session_date": session_date or vb.get("date"),
                    "subject": vb.get("subject"),
                    "source_url": pdf_url,
                    "votes": vb.get("votes", {}),
                })

    print(f"\n=== Przetwarzanie {len(all_raw_votes)} głosowań ===")
    votes_parsed = parse_vote_results(all_raw_votes)

    print(f"\n=== Budowanie struktury danych ===")
    name_to_club = COUNCILORS
    kadencja_id = "2024-2029"
    councilor_profiles = build_councilor_profiles(votes_parsed, kadencja_id)

    # Compute club agreement
    club_agreement = compute_club_agreement(votes_parsed, name_to_club)
    for profile in councilor_profiles:
        kd = profile["kadencje"][kadencja_id]
        kd["zgodnosc_z_klubem"] = club_agreement.get(profile["name"], 0.0)

    # Compute rebellions
    rebellions = compute_rebellions(votes_parsed, name_to_club)
    for profile in councilor_profiles:
        kd = profile["kadencje"][kadencja_id]
        r = rebellions.get(profile["name"], [])
        kd["rebellion_count"] = len(r)
        kd["rebellions"] = r

    # Compute similarity
    sim_top, sim_bottom = compute_similarity(votes_parsed, name_to_club)

    # Build sessions summary
    sessions_summary = build_sessions_summary(votes_parsed)

    # Build clubs dict: {club_name: member_count}
    clubs_count = {}
    for name, club in name_to_club.items():
        clubs_count[club] = clubs_count.get(club, 0) + 1

    # Build flat councilor list for data.json (used by ranking/voting tables)
    councilors_flat = []
    for profile in councilor_profiles:
        kd = profile["kadencje"][kadencja_id]
        councilors_flat.append({
            "name": profile["name"],
            "club": kd["club"],
            "frekwencja": kd["frekwencja"],
            "aktywnosc": kd["aktywnosc"],
            "zgodnosc_z_klubem": kd["zgodnosc_z_klubem"],
            "votes_za": kd["votes_za"],
            "votes_przeciw": kd["votes_przeciw"],
            "votes_wstrzymal": kd["votes_wstrzymal"],
            "votes_brak": kd["votes_brak"],
            "votes_nieobecny": kd["votes_nieobecny"],
            "votes_total": kd["votes_total"],
            "rebellion_count": kd["rebellion_count"],
            "rebellions": kd["rebellions"],
        })

    # Build output structure
    output = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kadencja_id,
        "kadencje": [
            {
                "id": kadencja_id,
                "label": KADENCJE[kadencja_id]["label"],
                "clubs": clubs_count,
                "sessions": sessions_summary,
                "total_sessions": len(sessions_summary),
                "total_votes": len(votes_parsed),
                "total_councilors": len(councilors_flat),
                "councilors": councilors_flat,
                "votes": votes_parsed,
                "similarity_top": sim_top,
                "similarity_bottom": sim_bottom,
            }
        ],
    }

    # Save data.json
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    save_split_output(output, output_path)

    size_kb = Path(output_path).stat().st_size / 1024
    print(f"\n=== Wyniki ===")
    print(f"Sesji: {len(sessions_summary)}")
    print(f"Głosowań: {len(votes_parsed)}")
    print(f"Radnych: {len(councilor_profiles)}")
    print(f"\nZapisano: {output_path} ({size_kb:.1f} KB)")

    # Save profiles.json
    profiles_output = {
        "profiles": councilor_profiles,
    }
    Path(profiles_path).parent.mkdir(parents=True, exist_ok=True)
    with open(profiles_path, "w", encoding="utf-8") as f:
        json.dump(profiles_output, f, ensure_ascii=False, indent=2)
    profiles_kb = Path(profiles_path).stat().st_size / 1024
    print(f"Zapisano: {profiles_path} ({profiles_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Scraper danych głosowań Rady Miasta Łodzi"
    )
    parser.add_argument(
        "--output", default="docs/data.json",
        help="Ścieżka do pliku wyjściowego (domyślnie: docs/data.json)"
    )
    parser.add_argument(
        "--profiles", default="docs/profiles.json",
        help="Ścieżka do profili (domyślnie: docs/profiles.json)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Tryb debug — drukuje surowy tekst z pierwszego PDF"
    )
    parser.add_argument(
        "--pdf-dir", default=None,
        help="Katalog cache PDF-ów (default: pdfs obok cwd). "
             "Pipeline przekazuje scratch dir żeby PDF-y i parsed_votes/ "
             "JSON-y persystowały między runami poza monorepo."
    )
    args = parser.parse_args()

    try:
        scrape(args.output, args.profiles, pdf_dir=args.pdf_dir, debug=args.debug)
    except KeyboardInterrupt:
        print("\n\nPrzerwano przez użytkownika.")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nBŁĄD: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


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
