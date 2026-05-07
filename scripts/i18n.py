"""
Lokalizacja UI Radoskop dla miast spoza Polski.

Domyślny język UI w template/index.html to polski. Dla miast z config.locale
== "en" (Praga i ewentualne kolejne) stosujemy słownik podstawień PL → EN.
Robimy to przez prosty string.replace, więc kolejność wpisów ma znaczenie:
najpierw długie frazy, potem krótkie. Stringi z znakiem placeholder ${...}
zostają nietknięte (replace jest exact-match na pełnym tekście).

Lokalne nazwy własne (radni, kluby, tematy uchwał) zostają w języku miasta —
te przychodzą z scrapera, nie z template. Tłumaczymy WYŁĄCZNIE etykiety UI.
"""

from __future__ import annotations


# Tłumaczenia ułożone od najdłuższych do najkrótszych żeby uniknąć podwójnych
# trafień (np. "Sesje" trafiło by w "Sesje rady" gdybyśmy najpierw zamienili
# słowo, a potem frazę).
PL_TO_EN: list[tuple[str, str]] = [
    # ── Nawigacja i taby ─────────────────────────────────────────────
    ("Rada Miasta {{CITY_GENITIVE}}", "{{CITY_NAME}} City Assembly"),
    ("Najbardziej aktywni radni", "Most active councilors"),
    ("Najczęściej głosują tak samo", "Most often vote the same"),
    ("Najrzadziej głosują tak samo", "Least often vote the same"),
    ("Profile radnych", "Councilor profiles"),
    ("Ranking radnych", "Councilor ranking"),
    ("Kto z kim głosuje", "Who votes with whom"),
    ("Wszystkie wyniki", "All results"),
    ("Lista interpelacji i zapytań", "Interpellations and questions"),
    ("Interpelacje", "Interpellations"),
    ("interpelacji", "interpellations"),
    ("Sprawdź frekwencję", "Check attendance"),
    ("Sprawdź na Radoskopie", "Check on Radoskop"),
    ("Strona główna", "Home"),
    ("Powrót na", "Back to"),
    ("Dane niepełne", "Partial data"),
    ("Dane z protokołów BIP", "Data from official records"),
    ("Informacje o źródłach danych", "Information about data sources"),
    ("Jak głosują radni", "How councilors vote"),
    ("Pobierz kartę", "Download card"),
    ("Lista członków z rankingiem frekwencji", "List of members ranked by attendance"),
    ("najczęściej głosujący wbrew klubowi", "most often voting against their club"),
    ("porównanie z innymi miastami", "comparison with other cities"),
    ("porównanie z innymi klubami w radzie", "comparison with other clubs in the assembly"),
    ("porównanie z innymi radnymi z tego klubu", "comparison with other councilors in this club"),
    ("porównanie wskaźników z innymi miastami", "metric comparison with other cities"),
    ("Charakter serwisu", "Service character"),
    ("Reklamy", "Advertising"),
    ("Twoje prawa", "Your rights"),
    ("Administrator danych", "Data controller"),
    ("Jakie dane zbieramy", "What data we collect"),
    ("Pliki cookies", "Cookies"),
    ("Cel i podstawa prawna przetwarzania", "Purpose and legal basis of processing"),
    ("Regulamin serwisu Radoskop", "Radoskop terms of service"),
    ("Raporty Radoskop", "Radoskop reports"),
    ("Źródło", "Source"),
    ("Wstęp", "Introduction"),
    ("aktywność", "activity"),
    ("frekwencję", "attendance"),
    ("frekwencji", "attendance"),
    ("budżet za", "budget for"),
    ("przyjętych", "passed"),
    ("Są to informacje publiczne", "These are public information"),
    ("dotyczące działalności", "regarding the activity of"),
    ("mają charakter szacunkowy", "are estimates"),
    ("ewentualne błędy w danych", "possible errors in data"),
    ("dlatego nie wyświetlamy banera zgody", "this is why we do not show a consent banner"),
    ("narzędzia analitycznego hostowanego", "analytics tool hosted"),
    ("na własnym serwerze", "on our own server"),
    ("Możesz wyczyścić cookies", "You can clear cookies"),
    ("nie ładujemy SPA appki", "we do not load the SPA"),
    ("pokazujemy treść i kończymy init", "we show content and finalize init"),
    ("aby dane były aktualne i poprawne", "to keep data accurate and up to date"),
    ("ponoszimy odpowiedzialności za", "are not liable for"),
    ("czasową niedostępność Serwisu", "temporary service unavailability"),
    ("dostępu do swoich danych", "access to your data"),
    ("Jeśli oryginalny URL miał", "If the original URL had"),
    ("Jeśli URL miał", "If the URL had"),
    ("dokumenty budżetowe", "budget documents"),
    ("klubów i rady miasta", "clubs and city assembly"),
    ("do których Serwis zawiera odnośniki", "to which the service links"),
    ("działanie serwisów zewnętrznych", "third party service operation"),
    ("mogą być swobodnie wykorzystywane", "may be freely used"),
    ("mandat zakończony przed dostępnym okresem", "mandate ended before the available period"),
    ("by uzyskać pełną listę", "to get the full list"),
    ("analiza klubów", "club analysis"),
    ("głosowania i aktywność", "voting and activity"),
    ("głosowań", "votes"),
    ("głosów", "votes"),
    ("głosowania", "votes"),
    ("Polityka prywatności", "Privacy policy"),
    ("Polityce prywatności", "Privacy policy"),
    ("Pytania i uwagi dotyczące Serwisu:", "Questions and feedback about the service:"),
    ("Pełny raport: Rada Miasta", "Full report: City Assembly"),
    ("Protokół głosowania (BIP) ↗", "Voting record (source) ↗"),
    ("Protokół głosowania", "Voting record"),
    ("Dane źródłowe:", "Data sources:"),
    ("Raporty klubów", "Club reports"),
    ("Raporty radnych", "Councilor reports"),
    ("Co zawiera raport radnego?", "What does the councilor report contain?"),
    ("Co zawiera raport klubu?", "What does the club report contain?"),
    ("Aktualności", "News"),

    # ── Filtry i sortowania ─────────────────────────────────────────
    ("Szukaj głosowań po temacie...", "Search votes by topic..."),
    ("Szukaj w interpelacjach...", "Search interpellations..."),
    ("Następna →", "Next →"),
    ("Następne →", "Next →"),
    ("Wszystkie kadencje", "All terms"),
    ("Wszystkie kluby", "All clubs"),
    ("Wszystkie", "All"),
    ("Wyczyść", "Clear"),
    ("Ładowanie sesji...", "Loading sessions..."),
    ("Sortuj po", "Sort by"),
    ("Filtruj", "Filter"),
    ("Pokaż więcej", "Show more"),
    ("Pokaż mniej", "Show less"),
    ("Powrót do listy", "Back to list"),
    ("Pozostałe", "Other"),

    # ── Etykiety w tabelach i kartach ───────────────────────────────
    ("Aktywność na sesjach", "Activity on sessions"),
    ("Aktywność mówców", "Speaker activity"),
    ("Frekwencja", "Attendance"),
    ("Aktywność", "Activity"),
    ("Zgodność z klubem", "Club alignment"),
    ("Podobieństwo", "Similarity"),
    ("Buntów", "Rebellions"),
    ("Sesji z wypowiedzią", "Sessions with speech"),
    ("Słów łącznie", "Words total"),
    ("Mówców", "Speakers"),
    ("Słowa", "Words"),
    ("Słów", "Words"),
    ("Głosów łącznie", "Votes total"),
    ("Głosowań budżetowych", "Budget votes"),
    ("Głosowań w sesji", "Votes in session"),
    ("Głosowania budżetowe", "Budget votes"),
    ("Głosowania, w których się różnili", "Votes where they differed"),
    ("Głosy wbrew klubowi", "Votes against club"),
    ("Głosy na sesjach", "Votes on sessions"),
    ("Głosy przeciw (%)", "Against (%)"),
    ("Głosy za (%)", "For (%)"),
    ("Głosowania", "Votes"),
    ("Głosowanie", "Vote"),
    ("Głosowań", "Votes"),
    ("Sesje", "Sessions"),
    ("Sesja", "Session"),
    ("Komisje", "Committees"),
    ("Komisji", "Committees"),
    ("Klub", "Club"),
    ("Kluby", "Clubs"),
    ("Kadencje", "Terms"),
    ("Kadencja", "Term"),
    ("Radny/a", "Councilor"),
    ("Radnych", "Councilors"),
    ("Radni", "Councilors"),
    ("Okręg wyborczy", "Constituency"),
    ("Okręg nr", "Constituency no."),
    ("Temat", "Topic"),
    ("Treść", "Content"),
    ("Odpowiedź", "Reply"),
    ("Data", "Date"),
    ("Wyniki", "Results"),
    ("Wynik", "Result"),

    # ── Wartości głosowań ────────────────────────────────────────────
    ("Brak głosu", "No vote"),
    ("Brak&nbsp;gł.", "No&nbsp;vote"),
    ("Brak gł.", "No vote"),
    ("Wstrzymał się", "Abstained"),
    ("Wstrzymała się", "Abstained"),
    ("Nieobecny", "Absent"),
    ("Nieobecna", "Absent"),
    ("Przyjęte", "Passed"),
    ("Przyjętych", "Passed"),
    ("Odrzucone", "Rejected"),
    ("Przeciw", "Against"),
    ("Za", "For"),

    # ── Stany i błędy ────────────────────────────────────────────────
    ("Brak danych dla tej kadencji", "No data for this term"),
    ("Brak danych o indywidualnych głosowaniach.", "No individual voting data."),
    ("Brak głosowań spełniających kryteria filtrowania.", "No votes match the filters."),
    ("Brak głosowań w tej kategorii.", "No votes in this category."),
    ("Nie znaleziono głosowania.", "Vote not found."),
    ("Nie znaleziono sesji.", "Session not found."),
    ("Błąd ładowania danych:", "Error loading data:"),
    ("Błąd ładowania głosowań.", "Error loading votes."),
    ("Błąd ładowania głosów.", "Error loading vote details."),
    ("Błąd ładowania interpelacji.", "Error loading interpellations."),
    ("Ładowanie głosowań...", "Loading votes..."),
    ("Ładowanie...", "Loading..."),

    # ── Akcje ───────────────────────────────────────────────────────
    ("Udostępnij na Facebooku", "Share on Facebook"),
    ("Udostępnij na X", "Share on X"),
    ("Udostępnij", "Share"),
    ("Porównaj radnych", "Compare councilors"),
    ("Porównanie radnych", "Compare councilors"),
    ("Porównanie metryk", "Metrics comparison"),
    ("Porównaj", "Compare"),
    ("Przełącz motyw jasny/ciemny", "Toggle light/dark theme"),
    ("Pobierz raport", "Download report"),
    ("Wybierz", "Select"),
    ("Wybrane", "Selected"),

    # ── Budżet ──────────────────────────────────────────────────────
    ("Budżet", "Budget"),
    ("Rok budżetowy:", "Budget year:"),
    ("Struktura wydatków", "Spending breakdown"),
    ("Oświata", "Education"),
    ("Nieruchomości", "Real estate"),

    # ── Stopka i prawne ─────────────────────────────────────────────
    ("Polityka prywatności", "Privacy policy"),
    ("Postanowienia ogólne", "General provisions"),
    ("Źródła danych", "Data sources"),
    ("Wskaźniki i statystyki", "Metrics and statistics"),
    ("Licencja i kod źródłowy", "License and source code"),
    ("Udostępnianie danych", "Data sharing"),
    ("Dane publiczne radnych", "Public councilor data"),
    ("Odpowiedzialność", "Liability"),
    ("Prywatność", "Privacy"),
    ("Kontakt", "Contact"),
    ("Przybliżona lokalizacja (kraj, miasto)", "Approximate location (country, city)"),
    ("Typ urządzenia, przeglądarka, system operacyjny", "Device type, browser, operating system"),

    # ── Status ──────────────────────────────────────────────────────
    ("(zakończony)", "(ended)"),
    ("Uchwała:", "Resolution:"),

    # Uwaga: nie tłumaczymy izolowanych słów bez kontekstu (np. " radnych",
    # " sesji ", " więcej") bo trafiają w środki polskich zdań w sekcjach
    # prawnych (Regulamin, Polityka prywatności) i tworzą Polenglish.
    # Te długie sekcje zostają po polsku w pierwszej iteracji.
]


def apply_locale(html: str, locale: str) -> str:
    """Zastosuj tłumaczenia dla danego locale.

    Locale "pl" (lub brak) → zwróć HTML bez zmian.
    Locale "en" → zastosuj słownik PL_TO_EN posortowany po długości
       polskiej frazy DESC. Sortowanie chroni przed sytuacją gdzie krótka
       fraza ("głosowania") zjadałaby fragment dłuższej ("Protokół
       głosowania (BIP)") zanim dłuższa zdąży się zmatchować.
    Pozostałe locale → na razie nieobsługiwane, zwrot bez zmian.
    """
    if not locale or locale.lower() == "pl":
        return html
    if locale.lower() == "en":
        out = html
        ordered = sorted(PL_TO_EN, key=lambda pair: -len(pair[0]))
        for pl, en in ordered:
            out = out.replace(pl, en)
        return out
    return html
