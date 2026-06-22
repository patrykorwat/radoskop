"""
Parser stenogramów sesji Rady Miasta Krakowa (BIP).

Stenogramy publikowane jako PDF na stronie Materiałów sesyjnych:
  https://www.bip.krakow.pl/?dok_id=190813

Format mówcy w PDF:
  Rola – p. I. Nazwisko
  np. "Przewodniczący obrad – p. J. Kosek"
      "Radny – p. T. Leśniak"
      "Radna – p. A. Szczepańska"
      "Sekretarz Miasta Krakowa – p. A. Fryczek"
      "Prezydent Miasta Krakowa Pan Aleksander Miszalski"

Zależności:
  pip install pdfplumber

Format wyjścia:
  [{"name": "Jakub Kosek", "statements": 56, "words": 4864}, ...]
"""

import re
import sys
from pathlib import Path

# Wspólny model stenogramów (agregacja tur, dominacja) z radoskop/scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_stenogram import aggregate_speakers  # noqa: E402


def normalize_ws(s: str) -> str:
    """Normalizuj białe znaki."""
    return re.sub(r"\s+", " ", s).strip()


# Regex matching speaker labels in Kraków stenograms.
# Pattern: "Role – p. I. Surname" at start of line.
# Also handles: "Prezydent Miasta Krakowa Pan Fullname" (without – p.)
SPEAKER_RE = re.compile(
    r"^("
    # Standard "Role – p. Initial. Surname" form
    r"(?:Przewodnicząc[aiy](?:\s+obrad)?|Wiceprzewodnicząc[aiy](?:\s+obrad)?|"
    r"Radn[ayi]|"
    r"Sekretarz(?:\s+Miasta\s+Krakowa)?|"
    r"Skarbnik(?:\s+Miasta\s+Krakowa)?|"
    r"Dyrektor(?:ka)?(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*|"
    r"Zastępc[aiy]\s+(?:Dyrektor[a-z]*|Prezydent[a-z]*|Burmistrz[a-z]*)(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*|"
    r"Prezydent\s+Miasta\s+Krakowa|"
    r"Wiceprezydent\s+Miasta\s+Krakowa|"
    r"Naczelni[a-z]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*|"
    r"Burmistrz(?:yni)?(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)*|"
    r"(?:Powiatow[a-z]+|Miejsk[a-z]+|Komendant[a-z]*|Inspektor[a-z]*|Rzeczni[a-z]+|Pełnomocni[a-z]+)"
    r"(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż]+)*"
    r")"
    # After role: " – p. I. Surname" or " Pan/Pani Fullname"
    r"\s+(?:–\s+p\.\s+|Pan\s+|Pani\s+)"
    r"[A-ZĄĆĘŁŃÓŚŹŻ]"  # Must start with capital letter
    r"[^:\n]{1,60}"  # Rest of name (up to colon)
    r")"  # End of capture group
    r"\s*$",  # Line must end here (no colon in Kraków format)
    re.MULTILINE
)

# Alternate pattern: some stenograms use "Role – p. I. Surname\n" without any special
# terminator, the name is simply followed by the speech on the next paragraph.
# We also need to handle speaker labels that end with a newline (no colon).


def extract_name_krakow(speaker_label: str, profiles_lookup: dict = None) -> str:
    """Extract full name from Kraków speaker label.

    Input: "Radny – p. T. Leśniak" or "Prezydent Miasta Krakowa Pan Aleksander Miszalski"
    Output: "Tomasz Leśniak" (resolved from profiles) or "T. Leśniak" (unresolved)
    """
    label = normalize_ws(speaker_label.strip())

    # Extract the name part after "– p." or "Pan/Pani"
    m = re.search(r'–\s+p\.\s+(.+)$', label)
    if m:
        raw_name = m.group(1).strip()
    else:
        m = re.search(r'(?:Pan|Pani)\s+(.+)$', label)
        if m:
            raw_name = m.group(1).strip()
        else:
            # Fallback: return last two words as name
            parts = label.split()
            raw_name = " ".join(parts[-2:]) if len(parts) >= 2 else label

    # Normalize spaced dashes: "Dydyńska – Czesak" → "Dydyńska-Czesak"
    raw_name = re.sub(r'\s*[–-]\s*', '-', raw_name)

    # Resolve abbreviated initial → full name via profiles
    # e.g. "T. Leśniak" → "Tomasz Leśniak"
    if profiles_lookup:
        resolved = _resolve_name(raw_name, profiles_lookup)
        if resolved:
            return resolved

    return raw_name


def _resolve_name(raw_name: str, profiles_lookup: dict) -> str | None:
    """Resolve abbreviated name like 'T. Leśniak' → 'Tomasz Leśniak'.

    profiles_lookup: {surname_lower: [full_name, ...], initial_surname_key: full_name}
    """
    parts = raw_name.split()
    if not parts:
        return None

    # Case 1: Full name already (no abbreviation) — "Aleksander Miszalski"
    if len(parts) >= 2 and not parts[0].endswith("."):
        # Check exact match
        candidate = raw_name
        if candidate in profiles_lookup.get("_exact", {}):
            return candidate
        return raw_name

    # Case 2: Abbreviated — "T. Leśniak" or "G. W. Stawowy"
    # Collect initials and surname
    initials = []
    surname_parts = []
    for p in parts:
        if p.endswith(".") and len(p) <= 3:
            initials.append(p[0].upper())
        elif p in ("–", "-"):
            # Dash in compound surname — attach to surrounding parts
            surname_parts.append("-")
        else:
            surname_parts.append(p)

    if not initials or not surname_parts:
        return None

    # Join surname, normalizing "Dydyńska - Czesak" → "Dydyńska-Czesak"
    surname = " ".join(surname_parts)
    surname = re.sub(r'\s*-\s*', '-', surname)
    surname_lower = surname.lower()

    # Look up by surname
    candidates = profiles_lookup.get("_by_surname", {}).get(surname_lower, [])
    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        # Disambiguate by initial(s)
        for c in candidates:
            c_parts = c.split()
            c_initials = [p[0].upper() for p in c_parts if not p[0].isupper() or p != surname_parts[0]]
            # Simple: match first initial
            if c_parts and c_parts[0][0].upper() == initials[0]:
                if len(initials) <= 1:
                    return c
                # Multi-initial: "G. W. Stawowy" — check second initial too
                if len(c_parts) >= 3 and len(initials) >= 2:
                    if c_parts[1][0].upper() == initials[1]:
                        return c

    return None


def build_profiles_lookup(profiles_data: dict) -> dict:
    """Build lookup structures from profiles.json data.

    Returns dict with:
      _by_surname: {surname_lower: [full_name, ...]}
      _exact: {full_name: True}
    """
    lookup = {"_by_surname": {}, "_exact": {}}
    for p in profiles_data.get("profiles", []):
        name = p["name"]
        lookup["_exact"][name] = True

        parts = name.split()
        if parts:
            # Surname = last part (or last two for double surnames)
            surname = parts[-1].lower()
            lookup["_by_surname"].setdefault(surname, []).append(name)

            # For compound surnames like "Pogoda-Tota", also index by
            # the full compound (already done above) and by each part
            if "-" in parts[-1]:
                for sub in parts[-1].split("-"):
                    sub_lower = sub.lower()
                    if sub_lower != surname:
                        lookup["_by_surname"].setdefault(sub_lower, []).append(name)

    return lookup


def _extract_pdf_text(path: str) -> str:
    """Wyciągnij tekst PDF + usuń nagłówki stron i numery stron."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("Zainstaluj pdfplumber: pip install pdfplumber")

    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

    if not text.strip():
        return ""

    # Remove page headers (repeated on every page)
    # Pattern: "XLVI SESJA RADY MIASTA KRAKOWA\n25 lutego 2026 r.\n"
    text = re.sub(
        r'^[IVXLCDM]+\s+SESJA\s+RADY\s+MIASTA\s+KRAKOWA\s*\n\s*\d{1,2}\s+\w+\s+\d{4}\s+r\.\s*\n',
        '\n', text, flags=re.MULTILINE
    )
    # Remove page numbers (standalone digits on a line)
    text = re.sub(r'^\s*\d{1,3}\s*$', '', text, flags=re.MULTILINE)
    return text


def _role_from_label(label: str) -> str:
    """Rola z etykiety mówcy: część przed '– p.' / 'Pan' / 'Pani'."""
    m = re.search(r'^(.*?)\s+(?:–\s+p\.|Pan\s+|Pani\s+)', normalize_ws(label))
    return m.group(1).strip() if m else ""


def parse_pdf_turns(path: str, profiles_lookup: dict = None) -> list[dict]:
    """Parse Kraków stenogram PDF → uporządkowane tury z pełną treścią.

    Zwraca [{"name", "text", "words", "role"}] w kolejności padania wypowiedzi.
    """
    text = _extract_pdf_text(path)
    if not text:
        print(f"  UWAGA: Pusty tekst PDF: {path}")
        return []

    segments = [(m.start(), m.end(), m.group(1)) for m in SPEAKER_RE.finditer(text)]
    if not segments:
        print(f"  UWAGA: Nie znaleziono mówców w {path}")
        return []

    turns = []
    for i, (start, end, label) in enumerate(segments):
        name = extract_name_krakow(label, profiles_lookup)
        speech_end = segments[i + 1][0] if i + 1 < len(segments) else len(text)
        speech = text[end:speech_end].strip()
        turns.append({
            "name": name,
            "role": _role_from_label(label),
            "text": speech,
            "words": len(speech.split()),
        })
    return turns


def parse_pdf(path: str, profiles_lookup: dict = None) -> list[dict]:
    """Agregat mówców (zgodny wstecz): [{"name", "statements", "words"}]."""
    turns = parse_pdf_turns(path, profiles_lookup)
    return [
        {"name": s["name"], "statements": s["statements"], "words": s["words"]}
        for s in aggregate_speakers(turns) if s["statements"] > 0
    ]


def parse_turns(path: str, profiles_lookup: dict = None) -> list[dict]:
    """Uporządkowane tury — tylko PDF (Kraków)."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return parse_pdf_turns(path, profiles_lookup)
    raise ValueError(f"Nieobsługiwany format: {p.suffix} (Kraków używa PDF)")


def parse_transcript(path: str, profiles_lookup: dict = None) -> list[dict]:
    """Parse Kraków transcript — only PDF supported."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return parse_pdf(path, profiles_lookup)
    else:
        raise ValueError(f"Nieobsługiwany format: {p.suffix} (Kraków używa PDF)")


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Użycie: python parse_stenogram.py <plik.pdf> [profiles.json]")
        sys.exit(1)

    profiles_lookup = None
    if len(sys.argv) >= 3:
        with open(sys.argv[2], encoding="utf-8") as f:
            profiles_lookup = build_profiles_lookup(json.load(f))

    speakers = parse_transcript(sys.argv[1], profiles_lookup)
    print(json.dumps(speakers, ensure_ascii=False, indent=2))
    print(f"\nŁącznie: {len(speakers)} mówców, "
          f"{sum(s['statements'] for s in speakers)} wypowiedzi, "
          f"{sum(s['words'] for s in speakers)} słów")
