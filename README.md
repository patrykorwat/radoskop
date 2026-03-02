# Radoskop Gdańsk

Otwarte narzędzie monitoringu głosowań Rady Miasta Gdańska.

Parsuje protokoły z BIP (generowane przez eSesja) i buduje dashboard z metrykami per radny: frekwencja, zgodność z klubem, głosy sprzeczne z linią klubową, macierz podobieństwa głosowań.

## Szybki start

```bash
pip install pdfplumber

# 1. Pobierz PDF-y protokołów z BIP Gdańsk do folderu pdfs/
# https://bip.gdansk.pl/rada-miasta-gdanska/Protokoly-z-sesji-Rady-Miasta-Gdanska,a,771

# 2. Sparsuj PDF-y
python scripts/parse_pdf.py pdfs/ --out data/

# 3. Zbuduj metryki i dane dashboardu
python scripts/build_metrics.py data/ --out dashboard/data.json

# 4. Odpal dashboard
cd dashboard && python -m http.server 8000
# Otwórz http://localhost:8000
```

## Struktura

```
radoskop/
├── scripts/
│   ├── parse_pdf.py       # Parser PDF-ów z eSesja/BIP
│   └── build_metrics.py   # Budowanie metryk per radny
├── data/                  # Sparsowane JSON-y sesji
├── dashboard/
│   ├── index.html         # Single-page dashboard (Chart.js)
│   └── data.json          # Dane dla dashboardu (generowane)
└── README.md
```

## Co mierzy

- **Frekwencja** — % sesji na których radny był obecny
- **Aktywność** — % głosowań w których oddał głos (za/przeciw/wstrzymał się vs brak głosu/nieobecny)
- **Zgodność z klubem** — % głosów zgodnych z większością klubu
- **Buntownicy** — głosowania gdzie radny głosował inaczej niż większość klubu
- **Macierz podobieństwa** — kto z kim głosuje najczęściej tak samo

## Źródło danych

Protokoły sesji Rady Miasta Gdańska publikowane w BIP. Format: PDF generowany przez system eSesja z głosowaniami imiennymi (wymóg ustawowy od 2018).

## Skalowanie na inne miasta

Parser działa z każdym PDF-em generowanym przez eSesja (używany przez setki gmin w Polsce). Żeby dodać inne miasto, wystarczy pobrać PDF-y z ich BIP-u.

## Licencja

MIT
