"""
Parser transkrypcji stenogramu sesji Rady m.st. Warszawy.

Obsługuje pliki DOCX (wersja_tekstowa) i PDF (fallback via PyMuPDF).
Wyciąga dane mówców: imię i nazwisko, liczbę wypowiedzi i słów.

Zależności:
  pip install python-docx pymupdf

Format wejścia:
  Pogrubiony tekst z rolą i nazwiskiem, np.:
    "Radny Wojciech Zabłocki:" — tekst wypowiedzi...
    "Przewodnicząca Rady m.st. Warszawy Ewa Malinowska-Grupińska:" — tekst...

Format wyjścia:
  [{"name": "Wojciech Zabłocki", "statements": 5, "words": 1234}, ...]
"""

import re
import sys
from pathlib import Path

# Wspólny model stenogramów (agregacja tur) z radoskop/scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_stenogram import aggregate_speakers  # noqa: E402

# Prefiksy ról do usunięcia (zostawiamy samo imię i nazwisko)
ROLE_PREFIXES = [
    r"Przewodnicząc[ay] Rady m\.st\. Warszawy\s+",
    r"Wiceprzewodnicząc[ay] Rady m\.st\. Warszawy\s+",
    r"Prezydent m\.st\. Warszawy\s+",
    r"Zastępc[ay] Prezydenta m\.st\. Warszawy\s+",
    r"Sekretarz m\.st\. Warszawy\s+",
    r"Skarbnik m\.st\. Warszawy\s+",
    r"Radn[ay]\s+",
    # Dyrektorzy, naczelnicy, etc. — multi-word titles before name
    r"(?:Stołeczn[a-z]+ Konserwator[a-z]* Zabytków|p\.o\. (?:Stołeczn[a-z]+ Konserwator[a-z]* Zabytków))\s+",
    r"Dyrektor(?:ka)?\s+(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+)+",
    r"Zastępc[ay] Dyrektor[a-z]*\s+(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+)+",
    r"Burmistrz(?:yni)?\s+(?:Dzielnicy\s+)?(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+\s+)*",
    r"Zastępc[ay] Burmistrz[a-z]*\s+(?:Dzielnicy\s+)?(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż-]+\s+)*",
    r"Naczelnik(?:czka)?\s+(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+)+",
    # Catch-all: any title ending in a known pattern before a capitalized name
    r"(?:Pełnomocni[a-z]+|Komendant[a-z]*|Rzeczni[a-z]+)\s+(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+)*",
]

# Kompiluj regex do ekstrakcji nazwy mówcy
ROLE_RE = re.compile("|".join(f"(?:{p})" for p in ROLE_PREFIXES))


def normalize_ws(s: str) -> str:
    """Normalizuj białe znaki — zamień wielokrotne spacje/newline na jedną spację."""
    return re.sub(r"\s+", " ", s).strip()


def extract_name(speaker_label: str) -> str:
    """Wyciągnij imię i nazwisko z etykiety mówcy, usuwając rolę."""
    label = normalize_ws(speaker_label.strip().rstrip(":"))
    # Próbuj usunąć znany prefiks
    cleaned = ROLE_RE.sub("", label, count=1).strip()
    if cleaned and cleaned != label:
        return cleaned
    # Fallback: jeśli żaden prefiks nie pasuje, zwróć całość
    return label


def count_words(text: str) -> int:
    """Policz słowa w tekście."""
    return len(text.split())


# Wzorzec PDF: linia zaczynająca się od roli/tytułu + nazwisko + ":"
_PDF_SPEAKER_RE = re.compile(
    r"^((?:Przewodnicząc[ay]|Wiceprzewodnicząc[ay]|Prezydent|Zastępc[ay] Prezydenta|"
    r"Sekretarz|Skarbnik|Radn[ay]|Dyrektor(?:ka)?|Naczelnik(?:czka)?|Burmistrz(?:yni)?|"
    r"Zastępc[ay] (?:Dyrektor|Burmistrz)|Stołeczn[a-z]+ Konserwator|"
    r"Pełnomocni[a-z]+|Komendant[a-z]*|Rzeczni[a-z]+)"
    r"[^:]{3,80}:)\s*(.*)$",
    re.MULTILINE
)


def _build_full_names(names) -> dict:
    """Mapa nazwisko → pełne 'Imię Nazwisko' (z nie-skróconych wariantów)."""
    full_names = {}
    for name in names:
        parts = name.split()
        if len(parts) >= 2 and not parts[0].endswith("."):
            full_names[parts[-1]] = name
    return full_names


def _resolve_name(name: str, full_names: dict) -> str:
    """Sprowadź wariant mówcy do nazwiska kanonicznego.

    Te same reguły co dawne _merge_speakers, ale per pojedyncza nazwa, żeby
    móc je nałożyć na każdą turę przed agregacją.
    """
    parts = name.split()
    # Skrót imienia: "E. Malinowska-Grupińska" → "Ewa Malinowska-Grupińska"
    if len(parts) >= 2 and parts[0].endswith(".") and parts[-1] in full_names:
        target = full_names[parts[-1]]
        if target != name:
            return target
    # Resztki ról (mała litera na początku lub frazy urzędowe) → wyłuskaj nazwisko
    if name and (name[0].islower() or
                 any(x in name for x in ["obowiązki", "Państwa", "Społecznych",
                                          "Obywatelskich", "Przestrzennego",
                                          "Prawny Biura", "Projektów"])):
        m = re.search(
            r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:[- ][A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)+)$",
            name)
        if m:
            return m.group(1)
    return name


def _raw_turns_docx(path: str) -> list[dict]:
    """Uporządkowane surowe tury z DOCX: [{name, text}] (przed scalaniem nazw)."""
    from docx import Document
    doc = Document(path)

    turns: list[dict] = []
    current_name = None
    current_parts: list[str] = []

    def flush():
        if current_name is not None:
            turns.append({
                "name": current_name,
                "text": " ".join(p for p in current_parts if p).strip(),
            })

    for para in doc.paragraphs:
        bold_text = ""
        rest_text = ""
        found_colon = False

        for run in para.runs:
            if run.bold and not found_colon:
                bold_text += run.text
                if ":" in run.text:
                    found_colon = True
                    parts = run.text.split(":", 1)
                    bold_text = bold_text[: bold_text.rfind(":")] if ":" in bold_text else bold_text
                    rest_text += parts[1] if len(parts) > 1 else ""
            else:
                rest_text += run.text

        bold_text = bold_text.strip()

        if bold_text and found_colon and len(bold_text) > 5:
            flush()
            current_name = extract_name(bold_text)
            current_parts = [rest_text.strip()]
        else:
            full_text = (bold_text + " " + rest_text).strip() if bold_text else rest_text.strip()
            if current_name is not None:
                current_parts.append(full_text)

    flush()
    return turns


def _raw_turns_pdf(path: str) -> list[dict]:
    """Uporządkowane surowe tury z PDF: [{name, text}] (przed scalaniem nazw)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "Zainstaluj PyMuPDF: pip install pymupdf\n"
            "(Alternatywnie: brew install poppler && pip install pdftotext)"
        )

    doc = fitz.open(path)
    text = ""
    for page in doc:
        text += page.get_text("text") + "\n"
    doc.close()

    segments = [(m.start(), m.group(1), m.group(2)) for m in _PDF_SPEAKER_RE.finditer(text)]
    turns = []
    for i, (pos, label, first_line) in enumerate(segments):
        name = extract_name(normalize_ws(label))
        end_pos = segments[i + 1][0] if i + 1 < len(segments) else len(text)
        speech = first_line + " " + text[pos + len(label) + len(first_line):end_pos]
        turns.append({"name": name, "text": normalize_ws(speech)})
    return turns


def parse_turns(path: str) -> list[dict]:
    """Uporządkowane tury z pełną treścią: [{name, text, words}] (DOCX/PDF)."""
    p = Path(path)
    if p.suffix.lower() == ".docx":
        raw = _raw_turns_docx(path)
    elif p.suffix.lower() == ".pdf":
        raw = _raw_turns_pdf(path)
    else:
        raise ValueError(f"Nieobsługiwany format: {p.suffix}")

    full_names = _build_full_names({t["name"] for t in raw})
    out = []
    for t in raw:
        name = _resolve_name(t["name"], full_names)
        text = (t["text"] or "").strip()
        out.append({"name": name, "text": text, "words": count_words(text)})
    return out


def parse_docx(path: str) -> list[dict]:
    """Agregat mówców z DOCX (zgodny wstecz): [{name, statements, words}]."""
    return [
        {"name": s["name"], "statements": s["statements"], "words": s["words"]}
        for s in aggregate_speakers(parse_turns(path)) if s["statements"] > 0
    ]


def parse_pdf(path: str) -> list[dict]:
    """Agregat mówców z PDF (zgodny wstecz): [{name, statements, words}]."""
    return [
        {"name": s["name"], "statements": s["statements"], "words": s["words"]}
        for s in aggregate_speakers(parse_turns(path)) if s["statements"] > 0
    ]


def parse_transcript(path: str) -> list[dict]:
    """Parsuj transkrypcję — auto-detect DOCX/PDF (agregat mówców)."""
    p = Path(path)
    if p.suffix.lower() in (".docx", ".pdf"):
        return [
            {"name": s["name"], "statements": s["statements"], "words": s["words"]}
            for s in aggregate_speakers(parse_turns(path)) if s["statements"] > 0
        ]
    raise ValueError(f"Nieobsługiwany format: {p.suffix}")


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Użycie: python parse_stenogram.py <plik.docx|plik.pdf>")
        sys.exit(1)

    speakers = parse_transcript(sys.argv[1])
    print(json.dumps(speakers, ensure_ascii=False, indent=2))
    print(f"\nŁącznie: {len(speakers)} mówców, "
          f"{sum(s['statements'] for s in speakers)} wypowiedzi, "
          f"{sum(s['words'] for s in speakers)} słów")
