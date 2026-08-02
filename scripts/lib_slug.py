"""Kanoniczny slugifier Radoskopu — jedna implementacja dla wszystkich miast.

Podejście: dekompozycja kanoniczna Unicode (NFKD) + zrzut znaków łączących
pokrywa każdą literę, która MA dekompozycję zdefiniowaną w Unicode
(ą→a+ogonek, é→e+acute, ř→r+caron, ü→u+diaeresis...). To jednak NIE
wystarcza samo w sobie: część liter nie ma dekompozycji kanonicznej, bo ich
modyfikator nie jest znakiem łączącym. Najważniejsza jest ł/Ł (kreska
przekreślająca), przez którą historyczny slugify NFKD+ascii-ignore wycinał
literę w całości (Paweł → pawe; bug Warszawy i sejmików, fix 2026-06-05).
Analogicznie ß, ø, đ, æ, œ, þ, ð. Te litery transliterujemy jawnie PRZED
NFKD przez _OVERRIDES.

UWAGA: Kopenhaga celowo NIE używa tego modułu — duńska konwencja
transliteracji to ø→oe, å→aa (slugi typu casper-oehlers), a tu ø→o.

Wynik dla polskich nazwisk jest identyczny z dotychczasowymi tabelami
ąćęłńóśźż→acelnoszz w lib_esesja/build_profiles (zweryfikowane na żywych
profiles.json wszystkich miast PL, 2026-06-05).
"""

import re
import unicodedata

# Litery bez dekompozycji kanonicznej w Unicode — jawna transliteracja.
# Ukraińska cyrylica (KMU 2010): А→A, Б→B, В→V, Г→H, Ґ→G, Д→D, Е→E,
# Є→Ye, Ж→Zh, З→Z, И→Y, І→I, Ї→Yi, Й→Y, К→K, Л→L, М→M, Н→N, О→O,
# П→P, Р→R, С→S, Т→T, У→U, Ф→F, Х→Kh, Ц→Ts, Ч→Ch, Ш→Sh, Щ→Shch,
# Ь→'', Ю→Yu, Я→Ya. Rosyjska: Ё→Yo, Ъ→'', Ы→Y, Э→E.
_OVERRIDES = str.maketrans({
    "ł": "l", "Ł": "L",
    "ß": "ss",
    "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "þ": "th", "Þ": "Th",
    "ð": "d", "Ð": "D",
    # Ukraińska cyrylica
    "А": "A", "а": "a",
    "Б": "B", "б": "b",
    "В": "V", "в": "v",
    "Г": "H", "г": "h",
    "Ґ": "G", "ґ": "g",
    "Д": "D", "д": "d",
    "Е": "E", "е": "e",
    "Є": "Ye", "є": "ye",
    "Ж": "Zh", "ж": "zh",
    "З": "Z", "з": "z",
    "И": "Y", "и": "y",
    "І": "I", "і": "i",
    "Ї": "Yi", "ї": "yi",
    "Й": "Y", "й": "y",
    "К": "K", "к": "k",
    "Л": "L", "л": "l",
    "М": "M", "м": "m",
    "Н": "N", "н": "n",
    "О": "O", "о": "o",
    "П": "P", "п": "p",
    "Р": "R", "р": "r",
    "С": "S", "с": "s",
    "Т": "T", "т": "t",
    "У": "U", "у": "u",
    "Ф": "F", "ф": "f",
    "Х": "Kh", "х": "kh",
    "Ц": "Ts", "ц": "ts",
    "Ч": "Ch", "ч": "ch",
    "Ш": "Sh", "ш": "sh",
    "Щ": "Shch", "щ": "shch",
    "Ь": "",   "ь": "",
    "Ю": "Yu", "ю": "yu",
    "Я": "Ya", "я": "ya",
    # Rosyjska cyrylica (dodatkowe litery)
    "Ё": "Yo", "ё": "yo",
    "Ъ": "",   "ъ": "",
    "Ы": "Y",  "ы": "y",
    "Э": "E",  "э": "e",
})


def make_slug(name: str) -> str:
    """'Paweł Lech' → 'pawel-lech', 'Bettina Meißner' → 'bettina-meissner'."""
    s = str(name or "").translate(_OVERRIDES)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Po overrides + dekompozycji zostają już tylko znaki spoza znanych
    # alfabetów (CJK, arabski, emoji) — wycinamy jak historyczne implementacje.
    # Cyrylica jest transliterowana przez _OVERRIDES (ukraińska KMU 2010,
    # rosyjska), więc trafia do ASCII przed tym krokiem.
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    # Kolaps separatorów łapie też " - " w nazwiskach
    # (Śrubarczyk - Cichowska → srubarczyk-cichowska).
    s = re.sub(r"[\s_\-]+", "-", s)
    return s.strip("-")


def legacy_nfkd_slug(name: str) -> str:
    """Replika historycznego, BŁĘDNEGO slugify (NFKD + ascii-ignore bez
    overrides): "Paweł Lech" → "pawe-lech". Tylko do generowania map
    redirectów stary→kanoniczny (_redirects/profiles.json), żeby URL-e
    z indeksu Google dostały 301 zamiast 404."""
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().replace(" ", "-").replace("'", "")


_PL_TABLE = str.maketrans("ąćęłńóśźż", "acelnoszz")


def legacy_table_slug(name: str) -> str:
    """Replika historycznego slugify z tabelą PL BEZ kolapsu separatorów
    (lib_esesja/build_profiles do 2026-06-05): "Anna Mazur- Kałuża" →
    "anna-mazur--kaluza" (podwójny dywiz), kropki zostają. Do map
    redirectów — takie slugi siedzą w indeksie (np. Kielce)."""
    slug = str(name or "").lower().translate(_PL_TABLE)
    return slug.replace(" ", "-").replace("'", "")


def legacy_surname_first_slug(name: str) -> str:
    """Slug ze STAREJ kolejności "Nazwisko Imię" — z czasów gdy EsesjaScraper
    miał name_order="as_is" (do 2026-06-06) i zostawiał kolejność ze źródła.
    Bierze kanoniczną nazwę "Imię [Imię2] Nazwisko" i odtwarza slug, jaki
    miała strona przed swapem: nazwisko (ostatni token) wraca na przód, reszta
    bez zmian, potem make_slug. "Sylwia Bielawska" → "bielawska-sylwia";
    "Adam Łukasz Chmielewski" → "chmielewski-adam-lukasz". Tylko do
    _redirects/profiles.json — żeby URL-e radnych z indeksu Google dostały
    301 zamiast 404 po przejściu miast eSesja na "Imię Nazwisko".

    Zwraca "" dla jednoczłonowych nazw (nie ma czego odwracać). Dla nazw już
    poprawnych w obu kierunkach (np. miasta nie-eSesja) wynik jest po prostu
    innym slugiem tej samej osoby — wpis jest inertny, bo taki URL nigdy nie
    istniał w indeksie i worker 301-uje tylko przy braku strony na S3."""
    parts = str(name or "").split()
    if len(parts) < 2:
        return ""
    reordered = [parts[-1]] + parts[:-1]
    return make_slug(" ".join(reordered))
