"""
Radoskop — PDF Parser for eSesja voting protocols.
Parses BIP/eSesja PDFs from Gdańsk City Council into structured JSON.

Usage:
    python parse_pdf.py <pdf_file_or_directory> [--out data/]
"""

import pdfplumber
import re
import json
import sys
import os
import glob
from pathlib import Path


def parse_voting_pdf(pdf_path):
    """Parse a single eSesja voting protocol PDF into structured data."""

    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        first_page_text = ""
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                full_text += text + "\n"
                if i == 0:
                    first_page_text = text

    # Clean footer artifacts (with or without timestamp)
    full_text = re.sub(
        r'Wygenerowano za pomo[cć][aą] app\.esesja\.pl\s*(?:\n?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})?',
        '', full_text
    )
    # Remove any remaining bare timestamps (YYYY-MM-DD HH:MM:SS on their own line)
    full_text = re.sub(r'\n\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*\n', '\n', full_text)

    # Session metadata
    POLISH_MONTHS = {
        'stycznia': '01', 'lutego': '02', 'marca': '03', 'kwietnia': '04',
        'maja': '05', 'czerwca': '06', 'lipca': '07', 'sierpnia': '08',
        'września': '09', 'października': '10', 'listopada': '11', 'grudnia': '12',
    }

    # --- Date extraction ---
    # Try DD.MM.YYYY format first (newer PDFs: "Dnia 13.12.2024r.") — ONLY on first page
    # to avoid matching dates in footers/later pages
    session_date_match = re.search(r'[Dd]nia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', first_page_text)
    # Try Polish month name format: "w dniu 30 kwietnia 2020" or "w dniu16 maja 2024"
    session_date_polish = re.search(
        r'w dniu\s*(\d{1,2})\s+(' + '|'.join(POLISH_MONTHS.keys()) + r')\s+(\d{4})',
        full_text
    )
    # Try "Sesja w dniu DD miesiąc YYYY" or "Sesja RMG w dniu DD miesiąc YYYY"
    session_date_sesja = re.search(
        r'Sesja\s+(?:RMG\s+)?w dniu\s*(\d{1,2})\s+(' + '|'.join(POLISH_MONTHS.keys()) + r')\s+(\d{4})',
        full_text
    )
    # Try "Obrady rozpoczęto DD miesiąc YYYY"
    session_date_obrady = re.search(
        r'Obrady rozpoczęto\s+(\d{1,2})\s+(' + '|'.join(POLISH_MONTHS.keys()) + r')\s+(\d{4})',
        full_text
    )

    iso_date = None
    date_raw = None
    if session_date_match:
        d, m, y = session_date_match.group(1), session_date_match.group(2), session_date_match.group(3)
        date_raw = f"{d}.{m}.{y}"
        iso_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    elif session_date_polish:
        d = session_date_polish.group(1)
        m = POLISH_MONTHS[session_date_polish.group(2)]
        y = session_date_polish.group(3)
        date_raw = f"{d} {session_date_polish.group(2)} {y}"
        iso_date = f"{y}-{m}-{d.zfill(2)}"
    elif session_date_sesja:
        d = session_date_sesja.group(1)
        m = POLISH_MONTHS[session_date_sesja.group(2)]
        y = session_date_sesja.group(3)
        date_raw = f"{d} {session_date_sesja.group(2)} {y}"
        iso_date = f"{y}-{m}-{d.zfill(2)}"
    elif session_date_obrady:
        d = session_date_obrady.group(1)
        m = POLISH_MONTHS[session_date_obrady.group(2)]
        y = session_date_obrady.group(3)
        date_raw = f"{d} {session_date_obrady.group(2)} {y}"
        iso_date = f"{y}-{m}-{d.zfill(2)}"

    # --- Session number extraction ---
    # Format 1: "Protokół nr XXII" or "Protokół XXII" (older)
    session_num_match = re.search(r'Protokół\s+(?:nr\s+)?([IVXLCDM]+)', full_text)
    # Format 2: "IV Protokół" (newer IX kadencja)
    session_num_prefix = re.search(r'^([IVXLCDM]+)\s+Protokół', full_text, re.MULTILINE)
    # Format 3: "[ROMAN] Sesja w dniu" or "[ROMAN] Sesja RMG"
    session_num_sesja = re.search(r'^([IVXLCDM]+)\s+Sesja\s+(?:RMG|w dniu)', full_text, re.MULTILINE)
    # Format 4: Arabic number — "12 Sesja RMG" or "4 Sesja w dniu"
    session_id_match = re.search(r'(\d+)\s+Sesja\s+(?:RMG|w dniu)', full_text)
    # Format 5: Arabic before "Protokół" — "11 Protokół"
    session_id_proto = re.search(r'^(\d+)\s+Protokół', full_text, re.MULTILINE)

    # Determine roman numeral
    ARABIC_TO_ROMAN = {
        1:'I',2:'II',3:'III',4:'IV',5:'V',6:'VI',7:'VII',8:'VIII',9:'IX',10:'X',
        11:'XI',12:'XII',13:'XIII',14:'XIV',15:'XV',16:'XVI',17:'XVII',18:'XVIII',19:'XIX',20:'XX',
        21:'XXI',22:'XXII',23:'XXIII',24:'XXIV',25:'XXV',26:'XXVI',27:'XXVII',28:'XXVIII',29:'XXIX',30:'XXX',
        31:'XXXI',32:'XXXII',33:'XXXIII',34:'XXXIV',35:'XXXV',36:'XXXVI',37:'XXXVII',38:'XXXVIII',39:'XXXIX',40:'XL',
        41:'XLI',42:'XLII',43:'XLIII',44:'XLIV',45:'XLV',46:'XLVI',47:'XLVII',48:'XLVIII',49:'XLIX',50:'L',
        51:'LI',52:'LII',53:'LIII',54:'LIV',55:'LV',56:'LVI',57:'LVII',58:'LVIII',59:'LIX',60:'LX',
        61:'LXI',62:'LXII',63:'LXIII',64:'LXIV',65:'LXV',66:'LXVI',67:'LXVII',68:'LXVIII',69:'LXIX',70:'LXX',
        71:'LXXI',72:'LXXII',73:'LXXIII',74:'LXXIV',75:'LXXV',76:'LXXVI',77:'LXXVII',78:'LXXVIII',79:'LXXIX',80:'LXXX',
    }

    number_roman = None
    number_arabic = None

    if session_num_match:
        number_roman = session_num_match.group(1)
    elif session_num_prefix:
        number_roman = session_num_prefix.group(1)
    elif session_num_sesja:
        number_roman = session_num_sesja.group(1)

    if session_id_match:
        number_arabic = int(session_id_match.group(1))
    elif session_id_proto:
        number_arabic = int(session_id_proto.group(1))

    # If we have arabic but no roman, convert
    if not number_roman and number_arabic and number_arabic in ARABIC_TO_ROMAN:
        number_roman = ARABIC_TO_ROMAN[number_arabic]
    # If we have roman but no arabic, reverse-convert
    if number_roman and not number_arabic:
        roman_to_arabic = {v: k for k, v in ARABIC_TO_ROMAN.items()}
        number_arabic = roman_to_arabic.get(number_roman)

    # Reconstruct BIP download URL from filename pattern: YEAR_DOCID_name.pdf
    source_file = os.path.basename(pdf_path)
    source_url = None
    url_match = re.match(r'\d{4}_(\d+)_(.+)$', source_file)
    if url_match:
        doc_id, rest = url_match.groups()
        source_url = f"https://download.cloudgdansk.pl/gdansk-pl/d/{doc_id}/{rest}"

    session = {
        "source_file": source_file,
        "source_url": source_url,
        "date": iso_date,
        "date_raw": date_raw,
        "number_roman": number_roman,
        "number": number_arabic,
    }

    # Extract attendees
    attendees_match = re.search(
        r'Obecni:\n(.*?)(?=\d+\.\s+Sprawy regulaminowe)', full_text, re.DOTALL
    )
    attendees = []
    if attendees_match:
        for line in attendees_match.group(1).strip().split('\n'):
            name = re.sub(r'^\d+\.\s*', '', line.strip())
            # Skip empty, timestamps, and non-name entries
            if name and not re.match(r'\d{4}-\d{2}-\d{2}', name) and not re.search(r'\d{2}:\d{2}:\d{2}', name):
                attendees.append(name)

    session["attendees"] = attendees
    session["attendee_count"] = len(attendees)

    # Parse all votes
    vote_blocks = re.split(r'Głosowano w sprawie:?\n', full_text)[1:]

    votes = []
    for idx, block in enumerate(vote_blocks):
        vote = {"vote_index": idx}

        # Topic
        topic_match = re.match(r'(.*?)Wyniki głosowania', block, re.DOTALL)
        if topic_match:
            topic = topic_match.group(1).strip()
            topic = re.sub(r'\s+', ' ', topic).strip()
            vote["topic"] = topic

        # Druk number
        druk_match = re.search(r'\(druk\s+(\d+)\)', vote.get("topic", ""))
        if druk_match:
            vote["druk"] = int(druk_match.group(1))

        # Resolution number (e.g. XXIII/562/26)
        reso_match = re.search(r'([IVXLCDM]+/\d+/\d+)', block[:500])
        if reso_match:
            vote["resolution"] = reso_match.group(1)

        # Summary counts
        counts_match = re.search(
            r'ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYM[^\d:,]+:\s*(\d+),\s*BRAK G[ŁL]OSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)',
            block
        )
        if counts_match:
            vote["counts"] = {
                "za": int(counts_match.group(1)),
                "przeciw": int(counts_match.group(2)),
                "wstrzymal_sie": int(counts_match.group(3)),
                "brak_glosu": int(counts_match.group(4)),
                "nieobecni": int(counts_match.group(5)),
            }

        # Named votes
        named_parts = re.split(r'Wyniki imienne:?\n', block)
        if len(named_parts) > 1:
            named_text = named_parts[1]
            vote["named_votes"] = {}

            categories = [
                (r"ZA", "za"),
                (r"PRZECIW", "przeciw"),
                (r"WSTRZYM\S+\s+SI[EĘ]", "wstrzymal_sie"),
                (r"BRAK G[ŁL]OSU", "brak_glosu"),
                (r"NIEOBECNI", "nieobecni"),
            ]

            # Find all category positions actually present in text
            cat_positions = []
            seen_keys = set()
            for pattern, key in categories:
                m = re.search(pattern + r'\s*\(\d+\)', named_text)
                if m and key not in seen_keys:
                    cat_positions.append((m.start(), m.end(), pattern, key))
                    seen_keys.add(key)

            cat_positions.sort(key=lambda x: x[0])

            for idx_c, (start, hdr_end, pat, key) in enumerate(cat_positions):
                # Find where names start (after "LABEL (N)\n")
                hdr_match = re.search(
                    pat + r'\s*\(\d+\)\s*\n',
                    named_text[start:]
                )
                if not hdr_match:
                    vote["named_votes"][key] = []
                    continue

                names_start = start + hdr_match.end()

                # End is either the start of next category or end markers
                if idx_c < len(cat_positions) - 1:
                    names_end = cat_positions[idx_c + 1][0]
                else:
                    # Last category — end at next agenda item or end of text
                    rest = named_text[names_start:]
                    end_match = re.search(
                        r'\n\d+[\.\)]\s+|Głosowano w sprawie|Wygenerowano za pomocą',
                        rest
                    )
                    names_end = names_start + end_match.start() if end_match else len(named_text)

                names_text = named_text[names_start:names_end].strip()
                # Clean up: remove any remaining page-break artifacts
                names_text = re.sub(
                    r'Wygenerowano za pomo[cć][aą] app\.esesja\.pl\s*(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})?',
                    '', names_text
                )
                names_text = re.sub(r'\s+', ' ', names_text).strip()
                if names_text:
                    raw_names = [n.strip() for n in names_text.split(',') if n.strip()]
                    # Filter: valid councilor names only (2-4 words, no digits, reasonable length)
                    names = []
                    for n in raw_names:
                        # Must look like a name: 2-4 words, starts uppercase, no digits, ≤35 chars
                        words = n.split()
                        if (2 <= len(words) <= 4
                                and len(n) <= 35
                                and n[0].isupper()
                                and not re.search(r'\d', n)
                                and not any(c in n for c in '();./')):
                            names.append(n)
                    vote["named_votes"][key] = names
                else:
                    vote["named_votes"][key] = []

            # Categories not present in text had 0 count
            present_keys = {key for _, _, _, key in cat_positions}
            for _pat, key in categories:
                if key not in present_keys:
                    vote["named_votes"][key] = []

        if vote.get("topic"):
            votes.append(vote)

    session["votes"] = votes
    session["vote_count"] = len(votes)
    return session


def validate_session(session):
    """Validate parsed session data. Returns (ok_count, fail_count, errors)."""
    errors = []
    ok = 0
    for i, v in enumerate(session['votes']):
        c = v.get('counts', {})
        nv = v.get('named_votes', {})
        expected = sum(c.get(k, 0) for k in ['za', 'przeciw', 'wstrzymal_sie', 'brak_glosu', 'nieobecni'])
        actual = sum(len(nv.get(k, [])) for k in ['za', 'przeciw', 'wstrzymal_sie', 'brak_glosu', 'nieobecni'])
        if expected != actual:
            errors.append(f"Vote {i}: expected {expected} names, got {actual}")
        else:
            ok += 1
    return ok, len(errors), errors


def batch_parse(input_path, output_dir):
    """Parse one PDF or all PDFs in a directory. Returns list of sessions."""
    os.makedirs(output_dir, exist_ok=True)

    if os.path.isfile(input_path):
        pdf_files = [input_path]
    else:
        pdf_files = sorted(glob.glob(os.path.join(input_path, "*.pdf")))

    all_sessions = []
    skipped = 0
    for pdf_file in pdf_files:
        basename = os.path.basename(pdf_file)
        # Skip empty files and known non-parseable prefixes
        if os.path.getsize(pdf_file) == 0:
            skipped += 1
            continue
        if basename.startswith('sesji_'):
            skipped += 1
            continue

        # Quick text check — skip scanned PDFs
        try:
            with pdfplumber.open(pdf_file) as _pdf:
                sample = ""
                for _p in _pdf.pages[:3]:
                    _t = _p.extract_text()
                    if _t:
                        sample += _t
                if len(sample) < 100:
                    print(f"Skipping (scanned): {basename}")
                    skipped += 1
                    continue
        except Exception:
            skipped += 1
            continue

        print(f"Parsing: {basename} ... ", end="")
        try:
            session = parse_voting_pdf(pdf_file)
            ok, fail, errors = validate_session(session)
            print(f"OK ({session['vote_count']} votes, {ok}/{ok+fail} validated)")
            if errors[:3]:
                for e in errors[:3]:
                    print(f"  WARNING: {e}")
                if len(errors) > 3:
                    print(f"  ... and {len(errors)-3} more warnings")
            all_sessions.append(session)
        except Exception as e:
            print(f"FAILED: {e}")

    if skipped:
        print(f"\nSkipped {skipped} files (empty/scanned/invalid)")

    # Save individual session JSONs
    for session in all_sessions:
        name = session.get('date', 'unknown')
        out_file = os.path.join(output_dir, f"session_{name}.json")
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

    # Save combined
    combined_path = os.path.join(output_dir, "all_sessions.json")
    with open(combined_path, 'w', encoding='utf-8') as f:
        json.dump(all_sessions, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(all_sessions)} sessions, "
          f"{sum(s['vote_count'] for s in all_sessions)} votes")
    print(f"Saved to: {output_dir}")

    return all_sessions


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parse_pdf.py <pdf_file_or_dir> [--out data/]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_dir = "data"
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            output_dir = sys.argv[idx + 1]

    batch_parse(input_path, output_dir)
