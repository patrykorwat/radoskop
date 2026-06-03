# Pozyskanie budżetów do porównywarki miast

Moduł `budget` w porównywarce miast zapala się automatycznie, gdy miasto ma
`cities/<slug>/docs/budget.json`. Capability jest wykrywana w
`build_compare_index.py` (`load_budget_summary`), bez żadnej flagi w config.
Ten dokument opisuje skalowalne źródła danych budżetowych i mapowanie na
kanoniczny schemat.

## Schemat kanoniczny (budget.json)

Plik, który czyta frontend oraz `build_compare_index.py`:

```json
{
  "scraped_at": "2026-06-03T08:00:00Z",
  "source": "GUS BDL API v1 (unit 011212161011)",
  "currency": "zł",
  "totals": [
    {"year": 2024, "revenue": 7600000000, "expenditure": 8100000000,
     "deficit": -500000000, "estimated": false}
  ],
  "categories": {
    "2024": [{"name": "Oświata i wychowanie", "amount": 2043350175}, ...]
  }
}
```

`build_compare_index.py` bierze najnowszy rok nie-estymowany, liczy udziały
top-3 działów (koncentracja wydatków, porównywalna mimo różnych taksonomii) i
dokłada per capita gdy znana jest populacja. Waluty NIE są przeliczane kursem.

## Polska: GUS Bank Danych Lokalnych (BDL) API v1

Jedno źródło dla wszystkich polskich miast. Publiczne REST API, dane per gmina
(level 6, łącznie z miastami na prawach powiatu), roczne szeregi w zł.

Baza: `https://bdl.stat.gov.pl/api/v1`

Hierarchia, którą wykorzystuje `build_budget_gus.py`:

- Grupa `G425` "Wydatki budżetów gmin i miast na prawach powiatu":
  - temat `P2633` Wydatki z budżetu ogółem
  - temat `P2920` Wydatki ogółem wg działów Klasyfikacji Budżetowej (jedna
    zmienna na dział: Transport i łączność, Oświata i wychowanie, Administracja
    publiczna, Gospodarka komunalna, Pomoc społeczna, Rodzina, Kultura itd.)
- Dochody ogółem: analogiczny temat w grupie dochodów (`P2693`).

Endpointy potwierdzone na żywym API:

```
GET /units/search?name=Kraków&level=6        -> unit-id "011212161011"
GET /subjects?parent-id=G425                  -> lista tematów (działów)
GET /variables?subject-id=P2920               -> zmienne (id + measureUnitName "zł")
GET /data/by-unit/{unitId}?var-id={id}        -> wartości roczne {year, val, attrId}
```

Skalowanie: dodanie miasta wymaga tylko jego `unit-id` (12 cyfr, oparty na
TERYT). Skrypt sam odkrywa zmienne w temacie, więc nie pinujemy ID per dział.
Unit-id jest rozwiązywany automatycznie po nazwie miasta (`resolve_unit_id`,
z priorytetem kind: gmina miejska > miejsko-wiejska, odrzuca gminę wiejską o
tej samej nazwie) i cache'owany do `bdl_unit_id` w `config.json` przy
`--cache-config`, więc kolejne runy nie odpytują `units/search`.

Kolejność źródeł unit-id: `--unit-id` > `config.bdl_unit_id` > resolve po
`city_name`. Gdy wybrana jednostka nie ma danych budżetowych (np. kind=4,
miasto w gminie miejsko-wiejskiej — budżet jest na poziomie gminy), skrypt nie
zapisuje `budget.json`, więc capability `budget` się nie zapala fałszywie.

Limity: bez klucza ~5 req/s. Dla pełnego runu ustaw `BDL_CLIENT_ID` (nagłówek
`X-ClientId`). Jeden run miasta to kilkanaście requestów (kilka tematów ×
kilkanaście działów), więc cały kraj mieści się w limicie przy małym sleepie.

Uwaga klasyfikacyjna: BDL daje dane wykonane (sprawozdawcze, Rb-28S), nie plan.
To dobrze do porównań rok-do-roku. Dane pojawiają się z opóźnieniem ~połowa
roku następnego, więc najnowszy pełny rok bywa o jeden wstecz.

## Berlin i miasta niemieckie: daten.berlin.de

Berlin publikuje Doppelhaushalt jako dane maszynowe (xlsx/CSV) na portalu
Open Data, strukturyzowane Einzelplan / Kapitel / Hauptgruppe.

- Portal: `https://daten.berlin.de/datensaetze?tags=Haushaltsplan`
- Aktualny: "Doppelhaushalt 2026/2027" (CSV/xlsx pod otwartą licencją)
- Referencyjny schemat i ETL: repo `github.com/berlin/haushaltsdaten`
  (xlsx -> CSV -> Postgres), wzorzec na offenerhaushalt.de

Zaimplementowane w `cities/berlin/scripts/build_budget_berlin.py`. CSV to
płaska tabela tytułów budżetowych; adapter czyta kolumny po nazwie (DictReader):

- `Jahr` rok, `Titelart` (Einnahme-/Ausgabetitel), `BetragTyp` (Soll=plan /
  Ist=wykonanie), `Betrag` (EUR, format DE), `Hauptfunktionsbezeichnung`
  (plan funkcjonalny, odpowiednik polskich działów)

Mapowanie na schemat kanoniczny:

- `currency`: "€"
- `totals[rok]`: expenditure = Σ Ausgabe, revenue = Σ Einnahme,
  deficit = revenue - expenditure; `estimated: true` dla BetragTyp "Soll"
- `categories[rok]`: wydatki zgrupowane po Hauptfunktionsbezeichnung (porównywarka
  liczy udziały top-3, nie mapuje nazw między krajami)

URL CSV zmienia się co edycję Doppelhaushalt — podawaj `--csv-url` albo
`--csv-file`. `--self-test` waliduje parsowanie bez sieci.

Inne miasta DE mają własne portale Open Data (np. Hamburg, Köln) o podobnej
strukturze Hauptgruppe/Hauptfunktion; każde to osobny adapter
`build_budget_<city>.py`, ten sam schemat wyjścia. Fallback dla miast bez
portalu: offenerhaushalt.de.

## Pozostałe kraje

Praga ma już `cities/praha/docs/budget.json` (waluta Kč) jako wzorzec formatu.
Dla każdego nowego kraju zasada jest ta sama: dowolny adapter, byle wypluł
kanoniczny `budget.json`. Porównanie cross-currency jest poglądowe (oznaczone w
UI), miarodajny jest wydatek per capita.

## Wpięcie w pipeline

Budżety to dane roczne (sprawozdawcze), więc to krok okresowy, NIE co-scrape.
W `run_pipeline.py` gated flagą `--budgets` (lub env `RADOSKOP_BUILD_BUDGETS=1`):

- `build_budgets_pl()` iteruje włączone miasta PL i woła `build_budget_gus.py
  --city <slug> --cache-config` (resolve + cache unit-id),
- `build_budgets_foreign()` woła adaptery zagraniczne (Berlin),

oba PRZED `build_and_distribute_compare_index()`, więc świeże `budget.json`
wchodzą do compare-index w tym samym runie. Sugerowana kadencja: miesięcznie
(osobny harmonogram), nie przy każdym scrape głosowań.

## Dodanie budżetu do miasta (checklist)

1. Ustal źródło: PL -> GUS BDL (`build_budget_gus.py`, automatyczny przez
   `--budgets`); DE -> daten portal (adapter jak Berlin); inne -> adapter
   per miasto wypluwający kanoniczny schemat.
2. Wygeneruj `cities/<slug>/docs/budget.json`.
3. Nic więcej. `build_compare_index.py` wykryje capability `budget` przy
   najbliższym runie i moduł zapali się dla każdej pary, w której oba miasta
   mają budżet.
