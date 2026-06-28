"""Wspólna logika klubów radnych — jedno źródło prawdy dla "braku linii klubowej".

Etykieta niezrzeszonych jest lokalizowana per kraj (PL "Niezrzeszeni",
DE "fraktionslos", DA "Løsgængere"/"Uden for partierne"), ale scrapery mapują
ją na KANONICZNY kod klubu "NZ" przed zapisem (patrz configi miast z kluczem
"NZ", cities/berlin/scripts/scrape_sessions.py {"fraktionslos": "NZ"},
cities/copenhagen/scripts/scrape_kk.py Løsgængere → NZ). Warszawa historycznie
zapisuje literał "Niezrzeszeni". Nieznany klub to "?" lub puste.

Dlatego sprawdzamy KOD klubu, nie tłumaczenie — hardcode jednego języka
("Niezrzeszeni") nie działa dla miast zagranicznych.

Zgodność z klubem i bunty (rebellion / "głos wbrew klubowi") MUSZĄ pomijać te
kluby: radny bez linii klubowej nie ma wobec czego się buntować. Niezrzeszeni
to bukiet niezależnych bez wspólnego stanowiska, więc "większość" jest fikcją.
"""

# Kanoniczne markery braku linii klubowej (po .strip().lower()).
_NO_LINE_EXACT = frozenset({"", "?", "-", "nz", "n/d", "brak"})


def club_has_line(club) -> bool:
    """True gdy `club` to realny klub z linią (whip).

    False dla niezrzeszonych / niezależnych / nieznanych: kanoniczny kod "NZ",
    literał "Niezrzeszeni"/"Niezrzeszony" (dowolny język bazujący na tym
    rdzeniu), "?", "-", "n/d", "brak", pusty/None.
    """
    if not club:
        return False
    c = str(club).strip().lower()
    if c in _NO_LINE_EXACT:
        return False
    if c.startswith("niezrzesz"):
        return False
    return True


def is_unaffiliated(club) -> bool:
    """Negacja club_has_line — czytelniejsza w miejscach filtrujących."""
    return not club_has_line(club)
