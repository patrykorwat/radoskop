"""
Lokalizacja UI Radoskop dla miast spoza Polski.

Domyślny język UI w template/index.html to polski. Dla miast z config.locale
== "en" / "de" stosujemy odpowiedni słownik podstawień. Robimy to przez prosty
string.replace, więc kolejność wpisów ma znaczenie: najpierw długie frazy,
potem krótkie. Stringi z placeholderem ${...} zostają nietknięte (replace
jest exact-match na pełnym tekście).

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


PL_TO_DE: list[tuple[str, str]] = [
    # ── Nawigacja i taby ─────────────────────────────────────────────
    ("Jak głosują radni Miasta {{CITY_GENITIVE}}? Dane z protokołów BIP.",
     "Wie stimmen die Abgeordneten in {{CITY_NAME}} ab? Daten aus offiziellen Protokollen."),
    ("Jak głosują radni?", "Wie stimmen die Abgeordneten ab?"),
    ("Rada Miasta {{CITY_GENITIVE}}", "Abgeordnetenhaus von {{CITY_NAME}}"),
    ("wszystkie miasta", "alle Städte"),
    ("Dane źródłowe:", "Quelldaten:"),
    ("Raporty", "Berichte"),
    ("Polityka prywatności", "Datenschutz"),
    ("Regulamin", "AGB"),
    ("Najbardziej aktywni radni", "Aktivste Abgeordnete"),
    ("Najczęściej głosują tak samo", "Stimmen am häufigsten gleich ab"),
    ("Najrzadziej głosują tak samo", "Stimmen am seltensten gleich ab"),
    ("Profile radnych", "Profile der Abgeordneten"),
    ("Ranking radnych", "Ranking der Abgeordneten"),
    ("Kto z kim głosuje", "Wer stimmt mit wem"),
    ("Wszystkie wyniki", "Alle Ergebnisse"),
    ("Lista interpelacji i zapytań", "Anfragen und Anträge"),
    ("Interpelacje", "Schriftliche Anfragen"),
    ("interpelacji", "Anfragen"),
    ("Strona główna", "Startseite"),
    ("Powrót do listy", "Zurück zur Liste"),

    # ── Filtry i sortowania ─────────────────────────────────────────
    ("Szukaj głosowań po temacie...", "Abstimmungen nach Thema suchen..."),
    ("Szukaj w interpelacjach...", "In Anfragen suchen..."),
    ("Następna →", "Weiter →"),
    ("Następne →", "Weiter →"),
    ("Wszystkie", "Alle"),
    ("Sortuj po", "Sortieren nach"),
    ("Filtruj", "Filter"),
    ("Pokaż więcej", "Mehr anzeigen"),
    ("Pokaż mniej", "Weniger anzeigen"),
    ("Wyczyść", "Löschen"),
    ("Pozostałe", "Andere"),

    # ── Etykiety w tabelach i kartach ───────────────────────────────
    ("Aktywność na sesjach", "Aktivität in den Sitzungen"),
    ("Aktywność mówców", "Rede-Aktivität"),
    ("Frekwencja", "Anwesenheit"),
    ("Aktywność", "Aktivität"),
    ("Zgodność z klubem", "Fraktionsdisziplin"),
    ("Podobieństwo", "Ähnlichkeit"),
    ("Buntów", "Abweichungen"),
    ("Sesji z wypowiedzią", "Sitzungen mit Redebeitrag"),
    ("Słów łącznie", "Wörter insgesamt"),
    ("Mówców", "Redner"),
    ("Słowa", "Wörter"),
    ("Słów", "Wörter"),
    ("Wystąpień", "Wortmeldungen"),
    ("wystąpień", "Wortmeldungen"),
    ("Obecnych", "Anwesend"),
    ("Obecni", "Anwesend"),
    ("obecnych radnych", "anwesende Abgeordnete"),
    ("obecnych", "Anwesende"),
    ("Nieobecnych", "Abwesend"),
    ("Nieobecni", "Abwesend"),
    ("Głosowania", "Abstimmungen"),
    ("Głosowanie", "Abstimmung"),
    ("głosowań", "Abstimmungen"),
    ("Sesje", "Sitzungen"),
    ("Sesja", "Sitzung"),
    ("Komisje", "Ausschüsse"),
    ("Komisji", "Ausschüsse"),
    ("Klub", "Fraktion"),
    ("Kluby", "Fraktionen"),
    ("Kadencje", "Wahlperioden"),
    ("Kadencja", "Wahlperiode"),
    ("Radny/a", "Abgeordnete:r"),
    ("Radnych", "Abgeordnete"),
    ("Radni", "Abgeordnete"),
    ("Okręg wyborczy", "Wahlkreis"),
    ("Temat", "Thema"),
    ("Treść", "Inhalt"),
    ("Odpowiedź", "Antwort"),
    ("Data", "Datum"),
    ("Wyniki", "Ergebnisse"),
    ("Wynik", "Ergebnis"),

    # ── Wartości głosowań (gdy są imienne, np. namentliche Abstimmung) ──
    ("Brak głosu", "Nicht abgestimmt"),
    ("Wstrzymał się", "Enthalten"),
    ("Wstrzymała się", "Enthalten"),
    ("Nieobecny", "Abwesend"),
    ("Nieobecna", "Abwesend"),
    ("Przyjęte", "Angenommen"),
    ("Przyjętych", "Angenommen"),
    ("Odrzucone", "Abgelehnt"),
    ("Przeciw", "Dagegen"),
    ("Za", "Dafür"),

    # ── Stany i błędy ────────────────────────────────────────────────
    ("Brak danych dla tej kadencji", "Keine Daten für diese Wahlperiode"),
    ("Brak danych o indywidualnych głosowaniach.", "Keine Daten zu individuellen Abstimmungen."),
    ("Nie znaleziono głosowania.", "Abstimmung nicht gefunden."),
    ("Nie znaleziono sesji.", "Sitzung nicht gefunden."),
    ("Błąd ładowania danych:", "Fehler beim Laden der Daten:"),
    ("Ładowanie...", "Lädt..."),

    # ── Akcje ───────────────────────────────────────────────────────
    ("Udostępnij na Facebooku", "Auf Facebook teilen"),
    ("Udostępnij na X", "Auf X teilen"),
    ("Udostępnij", "Teilen"),
    ("Porównaj radnych", "Abgeordnete vergleichen"),
    ("Porównanie radnych", "Vergleich der Abgeordneten"),
    ("Porównaj", "Vergleichen"),
    ("Przełącz motyw jasny/ciemny", "Hell-/Dunkel-Modus"),

    # ── Stopka i prawne ─────────────────────────────────────────────
    ("Polityka prywatności", "Datenschutz"),
    ("Kontakt", "Kontakt"),

    # ── Skróty i drobne etykiety inline (Rede-Aktivität row) ─────────
    # Template ma `${statements} wyp. · ${words.toLocaleString('pl')} słów`,
    # apply_locale podmienia te fragmenty literałem w JS template literal.
    # Dłuższe frazy najpierw — bo apply_locale sortuje po długości DESC.
    ("Śr. słów/sesję", "⌀ Wörter/Sitzung"),
    ("wyp.", "Beitr."),
    ("słów", "Wörter"),
    ("toLocaleString('pl')", "toLocaleString('de')"),
]


PL_TO_CS: list[tuple[str, str]] = [
    # ── Nawigacja i taby ─────────────────────────────────────────────
    ("Jak głosują radni Miasta {{CITY_GENITIVE}}? Dane z protokołów BIP.",
     "Jak hlasují zastupitelé hl. m. {{CITY_NAME}}? Data z oficiálních zdrojů."),
    ("Jak głosują radni?", "Jak hlasují zastupitelé?"),
    ("Rada Miasta {{CITY_GENITIVE}}", "Zastupitelstvo města {{CITY_NAME}}"),
    ("wszystkie miasta", "všechna města"),
    ("Dane źródłowe:", "Zdrojová data:"),
    ("Raporty", "Reporty"),
    ("Polityka prywatności", "Zásady ochrany osobních údajů"),
    ("Regulamin", "Podmínky"),
    ("Najbardziej aktywni radni", "Nejaktivnější zastupitelé"),
    ("Najczęściej głosują tak samo", "Hlasují nejčastěji stejně"),
    ("Najrzadziej głosują tak samo", "Hlasují nejméně často stejně"),
    ("Profile radnych", "Profily zastupitelů"),
    ("Ranking radnych", "Žebříček zastupitelů"),
    ("Kto z kim głosuje", "Kdo s kým hlasuje"),
    ("Wszystkie wyniki", "Všechny výsledky"),
    ("Lista interpelacji i zapytań", "Interpelace a dotazy"),
    ("Interpelacje", "Interpelace"),
    ("interpelacji", "interpelací"),
    ("Strona główna", "Hlavní stránka"),
    ("Powrót do listy", "Zpět na seznam"),

    # ── Filtry i sortowania ─────────────────────────────────────────
    ("Szukaj głosowań po temacie...", "Hledat hlasování podle tématu..."),
    ("Szukaj w interpelacjach...", "Hledat v interpelacích..."),
    ("Następna →", "Další →"),
    ("Następne →", "Další →"),
    ("← Poprzednia", "← Předchozí"),
    ("Wszystkie", "Všechny"),
    ("Budżet", "Rozpočet"),
    ("Sortuj po", "Řadit podle"),
    ("Filtruj", "Filtrovat"),
    ("Pokaż więcej", "Zobrazit více"),
    ("Pokaż mniej", "Zobrazit méně"),
    ("Wyczyść", "Vymazat"),
    ("Pozostałe", "Ostatní"),

    # ── Etykiety w tabelach i kartach ───────────────────────────────
    ("Aktywność na sesjach", "Aktivita na zasedáních"),
    ("Aktywność mówców", "Aktivita řečníků"),
    ("Frekwencja", "Účast"),
    ("Aktywność", "Aktivita"),
    ("Zgodność z klubem", "Souhlas s klubem"),
    ("Podobieństwo", "Podobnost"),
    ("Buntów", "Vzpour"),
    ("Sesji z wypowiedzią", "Zasedání s vystoupením"),
    ("Słów łącznie", "Slov celkem"),
    ("Mówców", "Řečníků"),
    ("Słowa", "Slova"),
    ("Słów", "Slov"),
    ("Wystąpień", "Vystoupení"),
    ("wystąpień", "vystoupení"),
    ("Obecnych", "Přítomno"),
    ("Obecni", "Přítomní"),
    ("obecnych radnych", "přítomných zastupitelů"),
    ("obecnych", "přítomných"),
    ("Nieobecnych", "Nepřítomno"),
    ("Nieobecni", "Nepřítomní"),
    ("głosowań", "hlasování"),
    ("Głosy na sesjach", "Hlasy na zasedáních"),
    ("Głosy wbrew klubowi", "Hlasy proti klubu"),
    ("Głosy przeciw (%)", "Proti (%)"),
    ("Głosy za (%)", "Pro (%)"),
    ("Głosowania budżetowe", "Rozpočtová hlasování"),
    ("Głosowania, w których się różnili", "Hlasování, kde se lišili"),
    ("Głosowania", "Hlasování"),
    ("Głosowanie", "Hlasování"),
    ("Głosowań", "Hlasování"),
    ("Głosy", "Hlasy"),
    ("Sesje", "Zasedání"),
    ("Sesja", "Zasedání"),
    ("Ładowanie sesji...", "Načítání zasedání..."),
    ("Ładowanie głosowań...", "Načítání hlasování..."),
    ("Porównanie metryk", "Porovnání metrik"),
    ("Raporty klubów", "Zprávy klubů"),
    ("Raporty radnych", "Zprávy zastupitelů"),
    ("Pobierz kartę (PNG)", "Stáhnout kartu (PNG)"),
    ("Pobierz kartę", "Stáhnout kartu"),
    ("Protokół głosowania (BIP) ↗", "Záznam hlasování (zdroj) ↗"),
    ("Struktura wydatków", "Struktura výdajů"),
    ("Polityce prywatności", "Zásady ochrany osobních údajů"),
    ("Rok budżetowy:", "Rozpočtový rok:"),
    ("Komisje", "Výbory"),
    ("Komisji", "Výborů"),
    ("Klub", "Klub"),
    ("Kluby", "Kluby"),
    ("Kadencje", "Volební období"),
    ("Kadencja", "Volební období"),
    ("Radny/a", "Zastupitel/ka"),
    ("Radnych", "Zastupitelů"),
    ("Radni", "Zastupitelé"),
    ("Okręg wyborczy", "Volební obvod"),
    ("Temat", "Téma"),
    ("Treść", "Obsah"),
    ("Odpowiedź", "Odpověď"),
    ("Data", "Datum"),
    ("Wyniki", "Výsledky"),
    ("Wynik", "Výsledek"),

    # ── Wartości głosowań ────────────────────────────────────────────
    ("Brak głosu", "Nehlasoval"),
    ("Wstrzymał się", "Zdržel se"),
    ("Wstrzymała się", "Zdržela se"),
    ("Nieobecny", "Nepřítomen"),
    ("Nieobecna", "Nepřítomna"),
    ("Przyjęte", "Přijato"),
    ("Przyjętych", "Přijato"),
    ("Odrzucone", "Zamítnuto"),
    ("Przeciw", "Proti"),
    ("Za", "Pro"),

    # ── Stany i błędy ────────────────────────────────────────────────
    ("Brak danych dla tej kadencji", "Žádná data pro toto volební období"),
    ("Brak danych o indywidualnych głosowaniach.", "Žádná data o jednotlivých hlasováních."),
    ("Nie znaleziono głosowania.", "Hlasování nenalezeno."),
    ("Nie znaleziono sesji.", "Zasedání nenalezeno."),
    ("Błąd ładowania danych:", "Chyba načítání dat:"),
    ("Ładowanie...", "Načítání..."),

    # ── Akcje ───────────────────────────────────────────────────────
    ("Udostępnij na Facebooku", "Sdílet na Facebooku"),
    ("Udostępnij na X", "Sdílet na X"),
    ("Udostępnij", "Sdílet"),
    ("Porównaj radnych", "Porovnat zastupitele"),
    ("Porównanie radnych", "Porovnání zastupitelů"),
    ("Porównaj", "Porovnat"),
    ("Przełącz motyw jasny/ciemny", "Přepnout světlý/tmavý režim"),

    # ── Stopka i prawne ─────────────────────────────────────────────
    ("Polityka prywatności", "Zásady ochrany osobních údajů"),
    ("Kontakt", "Kontakt"),

    # ── Skróty i drobne etykiety inline (Aktivita projevů) ──────────
    # Dłuższe frazy najpierw — bo apply_locale sortuje po długości DESC.
    ("Śr. słów/sesję", "⌀ slov/zasedání"),
    ("wyp.", "výst."),
    ("słów", "slov"),
    ("toLocaleString('pl')", "toLocaleString('cs')"),
]


def apply_locale(html: str, locale: str) -> str:
    """Zastosuj tłumaczenia dla danego locale.

    Locale "pl" (lub brak) → zwróć HTML bez zmian.
    Locale "en" → słownik PL_TO_EN.
    Locale "de" → słownik PL_TO_DE (Berlin, w przyszłości Hamburg, Wien).

    Wymiana używa regex z negative lookbehind/lookahead na word characters,
    żeby pojedyncze słowa nie trafiały w identyfikatory JS lub CSS classes
    (np. "Interpelacje" wewnątrz `renderInterpelacje` nie zostanie podmienione).
    Granica word-char to litera/cyfra/_, więc "/interpelacje/" matchuje
    (slash to nie word char), "renderInterpelacje" nie matchuje.

    Sortowanie po długości DESC chroni przed kolizją krótka/długa fraza.
    Pozostałe locale → nieobsługiwane, zwrot bez zmian.
    """
    import re
    if not locale or locale.lower() == "pl":
        return html
    dictionaries = {
        "en": PL_TO_EN,
        "de": PL_TO_DE,
        "cs": PL_TO_CS,
    }
    d = dictionaries.get(locale.lower())
    if d is None:
        return html
    out = html
    ordered = sorted(d, key=lambda pair: -len(pair[0]))
    for pl, target in ordered:
        pattern = re.compile(rf"(?<!\w){re.escape(pl)}(?!\w)")
        out = pattern.sub(lambda _m, t=target: t, out)
    return out
