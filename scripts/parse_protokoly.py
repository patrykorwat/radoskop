"""
Radoskop — Parse session protocol PDFs to extract councilor statements.

Parses full session protocols (not voting PDFs) from BIP Gdańsk to extract:
- Who spoke at each session
- How many times each person spoke
- Word count per statement
- Topic context for each statement

Usage:
    python scripts/parse_protokoly.py [protokoly/] [--out data/protokoly/]
"""

import pdfplumber
import re
import json
import sys
import os
import glob
import unicodedata
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent.parent

# All known councilor names (both kadencje) for matching
KNOWN_COUNCILORS = {
    # VIII kadencja (2018-2024)
    "Łukasz Bejm", "Kamila Błaszczyk", "Wojciech Błaszkowski",
    "Joanna Cabaj", "Katarzyna Czerniewska", "Beata Dunajewska",
    "Piotr Dzik", "Piotr Gierszewski", "Anna Golędzinowska",
    "Michał Hajduk", "Henryk Hałas", "Barbara Imianowska",
    "Waldemar Jaroszewicz", "Beata Jankowiak", "Krystian Kłos",
    "Kazimierz Koralewski", "Andrzej Kowalczys", "Alicja Krasula",
    "Emilia Lodzińska", "Przemysław Majewski", "Przemysław Malak",
    "Bogdan Oleszek", "Agnieszka Owczarczak", "Jan Perucki",
    "Romuald Plewa", "Karol Rabenda", "Przemysław Ryś",
    "Andrzej Skiba", "Mateusz Skarbek", "Andrzej Stelmasiewicz",
    "Elżbieta Strzelczyk", "Cezary Śpiewak-Dowbór",
    "Lech Wałęsa", "Teresa Wasilewska", "Karol Ważny",
    # IX kadencja (2024-2029)
    "Agnieszka Bartków", "Jolanta Banach", "Sylwia Cisoń",
    "Żaneta Geryk", "Aleksander Jankowski", "Maximilian Kieturakis",
    "Marta Magott", "Marcin Makowski", "Marcin Mickun",
    "Tomasz Rakowski", "Sylwia Rydlewska-Kowalik", "Łukasz Świacki",
}

# Build name lookup: last name -> full name, first+last -> full name
NAME_LOOKUP = {}
for name in KNOWN_COUNCILORS:
    parts = name.split()
    last = parts[-1]
    # Map "Kowalczys" -> "Andrzej Kowalczys" etc.
    if last not in NAME_LOOKUP:
        NAME_LOOKUP[last] = name
    else:
        # Collision: store as list
        existing = NAME_LOOKUP[last]
        if isinstance(existing, list):
            existing.append(name)
        else:
            NAME_LOOKUP[last] = [existing, name]
    # Full name lookup
    NAME_LOOKUP[name] = name

POLISH_MONTHS = {
    'stycznia': '01', 'lutego': '02', 'marca': '03', 'kwietnia': '04',
    'maja': '05', 'czerwca': '06', 'lipca': '07', 'sierpnia': '08',
    'września': '09', 'października': '10', 'listopada': '11', 'grudnia': '12',
}


def normalize_text(text):
    """Remove extra whitespace, fix common OCR artifacts."""
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    return text


def extract_text_from_pdf(pdf_path):
    """Extract full text from a protocol PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n".join(pages)


def extract_session_metadata(text, filename):
    """Extract session number, date, and other metadata from protocol header."""
    meta = {"source_file": os.path.basename(filename)}

    # Session number: "PROTOKÓŁ NR XIV/2025"
    num_match = re.search(
        r'PROTOK[ÓO][ŁL]\s+(?:NR\s+)?([IVXLCDM]+)[/\s]',
        text[:2000], re.IGNORECASE
    )
    if num_match:
        meta["session_roman"] = num_match.group(1).upper()

    # Date: "w dniu 27 marca 2025 r." or "dnia 07.05.2024r."
    date_polish = re.search(
        r'(?:w dniu|dnia)\s+(\d{1,2})\s+(' + '|'.join(POLISH_MONTHS.keys()) + r')\s+(\d{4})',
        text[:2000]
    )
    date_numeric = re.search(
        r'(?:w dniu|dnia)\s+(\d{1,2})\.(\d{1,2})\.(\d{4})',
        text[:2000]
    )

    if date_polish:
        d = date_polish.group(1).zfill(2)
        m = POLISH_MONTHS[date_polish.group(2)]
        y = date_polish.group(3)
        meta["date"] = f"{y}-{m}-{d}"
    elif date_numeric:
        d = date_numeric.group(1).zfill(2)
        m = date_numeric.group(2).zfill(2)
        y = date_numeric.group(3)
        meta["date"] = f"{y}-{m}-{d}"

    return meta


def resolve_name(raw_name):
    """Try to match a raw name string to a known councilor."""
    raw_name = raw_name.strip()
    # Remove common prefixes
    raw_name = re.sub(
        r'^(radny|radna|pan|pani|przewodnicz[aą]c[ya]|wiceprzewodnicz[aą]c[ya])\s+',
        '', raw_name, flags=re.IGNORECASE
    )
    # Remove titles
    raw_name = re.sub(
        r'^(rady miasta|rady|rmg|komisji)\s+', '', raw_name, flags=re.IGNORECASE
    )
    raw_name = raw_name.strip()

    # Direct match
    if raw_name in NAME_LOOKUP:
        result = NAME_LOOKUP[raw_name]
        return result if isinstance(result, str) else result[0]

    # Try last name only
    parts = raw_name.split()
    if parts:
        last = parts[-1]
        if last in NAME_LOOKUP:
            result = NAME_LOOKUP[last]
            if isinstance(result, str):
                return result
            # Ambiguous — try first name
            first = parts[0] if len(parts) > 1 else ""
            for candidate in result:
                if candidate.startswith(first):
                    return candidate
            return result[0]  # default to first match

    # Fuzzy: try without diacritics
    def strip_diacritics(s):
        return ''.join(
            c for c in unicodedata.normalize('NFD', s)
            if unicodedata.category(c) != 'Mn'
        )

    raw_stripped = strip_diacritics(raw_name.lower())
    for known in KNOWN_COUNCILORS:
        if strip_diacritics(known.lower()) == raw_stripped:
            return known
        # Last name match
        known_last = strip_diacritics(known.split()[-1].lower())
        raw_parts = raw_stripped.split()
        if raw_parts and raw_parts[-1] == known_last:
            return known

    return None  # not a councilor


def extract_statements(text):
    """
    Extract councilor statements from protocol text.

    Real protocol formats observed:
    1. "FIRSTNAME LASTNAME – role" (ALL CAPS name, dash, role) — most common
    2. "Radna FIRSTNAME LASTNAME – role" (prefix + ALL CAPS)
    3. "Radna FIRSTNAME LASTNAME" (prefix + ALL CAPS, no dash)
    4. "Firstname Lastname – role" (Title Case, some protocols)
    """
    statements = []

    # Unicode uppercase letters including Polish
    UC = r'A-ZŻŹĆŃÓŁĘĄŚ'
    LC = r'a-zżźćńółęąś'

    # Pattern 1: ALL CAPS name followed by dash
    # "AGNIESZKA OWCZARCZAK – Przewodnicząca..."
    # "CEZARY ŚPIEWAK DOWBÓR – Radny..."
    # "KAZIMIERZ KORALEWSKI – radny..."
    ALLCAPS_DASH = re.compile(
        r'^([{uc}][{uc}]+(?:\s+[{uc}][{uc}]+){{1,3}})\s*[–—-]\s*'.format(uc=UC),
        re.MULTILINE
    )

    # Pattern 2: "Radna/Radny FIRSTNAME LASTNAME" (with or without dash)
    RADNY_ALLCAPS = re.compile(
        r'^[Rr]adn[yaeą]\s+([{uc}][{uc}]+(?:\s+[{uc}][{uc}]+){{1,3}})(?:\s*[–—-]|\s*$)'.format(uc=UC),
        re.MULTILINE
    )

    # Pattern 3: Title Case "Firstname Lastname – " for known councilors
    TITLE_DASH = re.compile(
        r'^([{uc}][{lc}]+(?:[- ][{uc}][{lc}]+)?\s+[{uc}][{lc}]+(?:-[{uc}][{lc}]+)?)\s*[–—-]\s*'.format(uc=UC, lc=LC),
        re.MULTILINE
    )

    # Pattern 4: "Radna/Radny Firstname Lastname – " Title case
    RADNY_TITLE = re.compile(
        r'^(?:[Rr]adn[yaeą]|[Pp]an[i]?)\s+([{uc}][{lc}]+(?:[- ][{uc}][{lc}]+)?\s+[{uc}][{lc}]+(?:-[{uc}][{lc}]+)?)\s*[–—-]\s*'.format(uc=UC, lc=LC),
        re.MULTILINE
    )

    full_text = text
    speaker_matches = []
    seen_positions = set()

    def add_match(m, name_group=1):
        pos = m.start()
        # Avoid duplicates within 10 chars
        for sp in seen_positions:
            if abs(sp - pos) < 10:
                return
        raw = m.group(name_group).strip()
        # Convert ALL CAPS to Title Case for resolution
        if raw == raw.upper() and len(raw) > 2:
            raw_title = raw.title()
            # Fix Polish diacritics that title() might break
            # "Śpiewak Dowbór" not "Śpiewak Dowbór" - title() handles this ok
        else:
            raw_title = raw
        resolved = resolve_name(raw_title)
        if resolved is not None:
            speaker_matches.append({
                "start": m.start(),
                "end": m.end(),
                "raw_name": raw,
                "resolved": resolved,
            })
            seen_positions.add(pos)

    # Apply all patterns
    for m in ALLCAPS_DASH.finditer(full_text):
        add_match(m)
    for m in RADNY_ALLCAPS.finditer(full_text):
        add_match(m)
    for m in TITLE_DASH.finditer(full_text):
        add_match(m)
    for m in RADNY_TITLE.finditer(full_text):
        add_match(m)

    # Sort by position
    speaker_matches.sort(key=lambda x: x["start"])

    # Extract text between consecutive speaker matches
    for i, sm in enumerate(speaker_matches):
        text_start = sm["end"]
        if i + 1 < len(speaker_matches):
            text_end = speaker_matches[i + 1]["start"]
        else:
            text_end = min(text_start + 5000, len(full_text))

        speech_text = full_text[text_start:text_end].strip()

        # Clean up: remove page numbers, footers
        speech_text = re.sub(r'^\d+\s*$', '', speech_text, flags=re.MULTILINE)
        speech_text = normalize_text(speech_text)

        if len(speech_text) > 10:  # minimum meaningful statement
            word_count = len(speech_text.split())
            statements.append({
                "speaker": sm["resolved"],
                "raw_name": sm["raw_name"],
                "text_preview": speech_text[:300] + ("..." if len(speech_text) > 300 else ""),
                "word_count": word_count,
                "char_count": len(speech_text),
            })

    return statements


def extract_attendance(text):
    """Extract attendance list from protocol."""
    attendees = []

    # Pattern: "Obecni:" or "Lista obecności" followed by names
    attendance_match = re.search(
        r'(?:Obecni|Lista obecno[śs]ci)[:\s]*\n(.*?)(?=\n\s*(?:Nieobecni|Porz[aą]dek|Punkt|Ad\.|Otwarcie))',
        text[:5000], re.DOTALL | re.IGNORECASE
    )

    if attendance_match:
        block = attendance_match.group(1)
        # Names are usually numbered: "1. Łukasz Bejm"
        for line in block.split('\n'):
            name_match = re.match(r'\s*\d+[\.\)]\s*(.+)', line.strip())
            if name_match:
                raw = name_match.group(1).strip()
                # Remove trailing dash or role
                raw = re.sub(r'\s*[-–—].*$', '', raw)
                resolved = resolve_name(raw)
                if resolved:
                    attendees.append(resolved)
                elif raw and len(raw) > 3:
                    attendees.append(raw)

    return attendees


def parse_protocol(pdf_path):
    """Parse a single session protocol PDF."""
    text = extract_text_from_pdf(pdf_path)
    if len(text) < 200:
        return None

    meta = extract_session_metadata(text, pdf_path)
    attendance = extract_attendance(text)
    statements = extract_statements(text)

    # Aggregate per-councilor stats
    speaker_stats = defaultdict(lambda: {"count": 0, "total_words": 0, "total_chars": 0})
    for s in statements:
        sp = s["speaker"]
        speaker_stats[sp]["count"] += 1
        speaker_stats[sp]["total_words"] += s["word_count"]
        speaker_stats[sp]["total_chars"] += s["char_count"]

    return {
        **meta,
        "text_length": len(text),
        "attendance": attendance,
        "attendance_count": len(attendance),
        "statements": statements,
        "statement_count": len(statements),
        "speaker_summary": {
            name: {
                "statement_count": stats["count"],
                "total_words": stats["total_words"],
                "avg_words": round(stats["total_words"] / stats["count"]) if stats["count"] > 0 else 0,
            }
            for name, stats in sorted(speaker_stats.items(), key=lambda x: -x[1]["count"])
        },
        "unique_speakers": len(speaker_stats),
    }


def batch_parse(input_path, output_dir):
    """Parse all protocol PDFs in a directory."""
    os.makedirs(output_dir, exist_ok=True)

    if os.path.isfile(input_path):
        pdf_files = [input_path]
    else:
        pdf_files = sorted(
            glob.glob(os.path.join(input_path, "*.pdf"))
        )

    if not pdf_files:
        print(f"No PDF files found in {input_path}")
        return []

    print(f"Found {len(pdf_files)} protocol PDFs to parse\n")

    all_protocols = []
    for pdf_file in pdf_files:
        basename = os.path.basename(pdf_file)
        print(f"Parsing: {basename} ... ", end="")

        try:
            result = parse_protocol(pdf_file)
            if result is None:
                print("SKIP (too short)")
                continue

            print(f"OK ({result['statement_count']} statements, "
                  f"{result['unique_speakers']} speakers)")

            # Show top speakers
            for name, stats in list(result["speaker_summary"].items())[:3]:
                print(f"    {name}: {stats['statement_count']} wypowiedzi, "
                      f"{stats['total_words']} słów")

            all_protocols.append(result)

        except Exception as e:
            print(f"FAILED: {e}")
            import traceback
            traceback.print_exc()

    # Save individual protocol JSONs
    for proto in all_protocols:
        date = proto.get("date", "unknown")
        session = proto.get("session_roman", "")
        name = f"protokol_{date}_{session}.json" if session else f"protokol_{date}.json"
        out_file = os.path.join(output_dir, name)
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(proto, f, ensure_ascii=False, indent=2)

    # Save combined
    combined_path = os.path.join(output_dir, "all_protokoly.json")
    with open(combined_path, 'w', encoding='utf-8') as f:
        json.dump(all_protocols, f, ensure_ascii=False, indent=2)

    # Build aggregate activity stats per councilor across all sessions
    activity = build_activity_stats(all_protocols)
    activity_path = os.path.join(output_dir, "activity.json")
    with open(activity_path, 'w', encoding='utf-8') as f:
        json.dump(activity, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(all_protocols)} protocols parsed")
    print(f"  {sum(p['statement_count'] for p in all_protocols)} statements extracted")
    print(f"Saved to: {output_dir}")
    print(f"Activity stats: {activity_path}")

    return all_protocols


def build_activity_stats(all_protocols):
    """Build aggregate speaking activity stats per councilor."""
    # Per-councilor aggregate
    councilor_activity = defaultdict(lambda: {
        "sessions_attended": 0,
        "sessions_spoke": 0,
        "total_statements": 0,
        "total_words": 0,
        "sessions": [],
    })

    for proto in all_protocols:
        date = proto.get("date", "unknown")
        session_roman = proto.get("session_roman", "")

        # Track attendance
        for name in proto.get("attendance", []):
            if name in KNOWN_COUNCILORS:
                councilor_activity[name]["sessions_attended"] += 1

        # Track speaking
        speakers_in_session = set()
        session_speaker_stats = defaultdict(lambda: {"count": 0, "words": 0})

        for stmt in proto.get("statements", []):
            speaker = stmt["speaker"]
            session_speaker_stats[speaker]["count"] += 1
            session_speaker_stats[speaker]["words"] += stmt["word_count"]
            speakers_in_session.add(speaker)

        for speaker in speakers_in_session:
            stats = session_speaker_stats[speaker]
            ca = councilor_activity[speaker]
            ca["sessions_spoke"] += 1
            ca["total_statements"] += stats["count"]
            ca["total_words"] += stats["words"]
            ca["sessions"].append({
                "date": date,
                "session": session_roman,
                "statements": stats["count"],
                "words": stats["words"],
            })

    # Compute averages and sort
    result = {}
    for name, data in sorted(councilor_activity.items(), key=lambda x: -x[1]["total_statements"]):
        spoke = data["sessions_spoke"]
        result[name] = {
            "sessions_attended": data["sessions_attended"],
            "sessions_spoke": spoke,
            "total_statements": data["total_statements"],
            "total_words": data["total_words"],
            "avg_statements_per_session": round(data["total_statements"] / spoke, 1) if spoke > 0 else 0,
            "avg_words_per_session": round(data["total_words"] / spoke) if spoke > 0 else 0,
            "sessions": data["sessions"],
        }

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parse RMG session protocol PDFs")
    parser.add_argument("input", nargs="?", default=str(BASE_DIR / "protokoly"),
                        help="PDF file or directory (default: protokoly/)")
    parser.add_argument("--out", default=str(BASE_DIR / "data" / "protokoly"),
                        help="Output directory (default: data/protokoly/)")
    args = parser.parse_args()

    batch_parse(args.input, args.out)
