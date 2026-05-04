# Sejmiki województw

Drugi poziom samorządu pokrywany przez Radoskop, obok rad miejskich. Każdy sejmik to organ stanowiący województwa (16 sztuk). Architektura jest celowo identyczna z miastami, żeby reuse skryptów `radoskop/scripts/build_*.py`, `generate_*.py` był możliwy bez kopiowania kodu.

## Status pilotażu

| Slug | BIP | Stolica | Radnych | Status scrape | Typ BIP |
|---|---|---|---|---|---|
| `mazowieckie` | https://www.mazovia.pl/pl/bip/sejmik/ | Warszawa | 51 | DZIAŁA (głosowania + interpelacje) | Mazovia BIP + PDFy eSesji |
| `dolnoslaskie` | https://bip.umwd.dolnyslask.pl/ | Wrocław | 36 | TBD | TBD (przypuszczalnie BIP statyczny) |
| `pomorskie` | https://bip.pomorskie.eu/ | Gdańsk | 33 | BLOCKED — wymaga Playwright | React SPA, Madkom CMS |

### Notatki o pomorskim BIP

`bip.pomorskie.eu` to React SPA z backendem Madkom CMS. Statyczny scrape nie zadziała, bo HTML strony to pusta skorupa, treść ładowana przez JS.

Co znalazłem podczas rozpoznania:

- `/api/menu/{id}` zwraca top-level menu (lista 30 pozycji), niezależnie od `id`. Czyli nie da się przez to nawigować w głąb drzewka.
- `/api/menu/main` chce ID liczbowy ale nie zwraca dzieci.
- Sprawdzone i nieobecne (HTTP 404): `/api/articles`, `/api/article/{id}`, `/api/page/{id}`, `/api/content/{id}`, `/api/node/{id}`, `/api/category/{id}/items`, `/api/menu/children/{id}`, `/api/menu/{id}/links`.
- Bundle JS jest 3 MB minified, ścieżki API budowane jako konkatenacje stringów, więc grep statyczny daje fragmenty bez kontekstu.

Sejmik korzysta z eSesji do streamu sesji (`app.esesja.pl/pomorskie/` istnieje), ale to też SPA. PDFy z imiennymi głosowaniami mogą być wystawione w BIP-ie pomorskim podobnie jak w mazowieckim, ale bez pełnej nawigacji w drzewku menu nie znajdę ich URL-i statycznie.

**Rekomendacja dla scrape**: użyć Playwright (chromium) na pipeline NAS, który już ma chromium w Dockerfile. Skrypt ładuje stronę BIP, czeka na render, ekstrahuje listę sesji z DOM, dla każdej sesji idzie głębiej. Wzorzec analogiczny do mazowieckiego, ale zamiast `urllib.request` używa headless browser. Czas na implementację: 2 do 3 godzin. Cache na dysku jako HTML + DOM snapshots żeby nie odpalać chromium za każdym razem dla tej samej strony.

**Alternatywa**: kontynuować inżynierię wsteczną API Madkoma. Otworzyć BIP w przeglądarce z DevTools i Network tab, zobaczyć dokładnie które endpointy są wywoływane przy nawigacji do "Sejmik → Sesje → konkretna sesja". Jeśli dany endpoint nie wymaga auth i odpowiada JSON, scraper bez Playwright działa.

Pozostałe 13 województw są w `radoskop/data/sejmiki-meta.csv` ze statusem `planned`. Pojawiają się w manifeście `/swiezosc/data.json` i kafelkach na stronie głównej.

## Struktura katalogu

```
radoskop/sejmiki/{slug}/
  config.json        # samorzad_type=wojewodztwo, rada_name, site_url, bip_url, councilor_count
  scripts/           # scrape_glosowania.py, scrape_interpelacje.py, lokalne pomocniki
  docs/              # kadencja-{id}.json, interpelacje.json, aktualnosci.json, profiles.json, data.json, index.html
```

Subdomena: `{slug}.radoskop.pl` (np. `mazowieckie.radoskop.pl`). Single-level, więc istniejący wildcard `*.radoskop.pl` w Cloudflare DNS i Worker route ją obejmują bez dodatkowych wpisów. Sluga województw są przymiotnikami (mazowieckie, dolnoslaskie...) a sluga miast rzeczownikami (gdansk, krakow...), nie ma kolizji.

## Identyczny schemat danych jak miasta

Pliki w `docs/` mają te same nazwy i top level shape co w `radoskop/cities/{slug}/docs/`. Konsumenci (build_freshness, build_*_index, generate_main_manifest) skanują oba katalogi i odróżniają poziom przez pole `samorzad_type` w config.json (`miasto` vs `wojewodztwo`).

## Playbook implementacji scrape per sejmik

### Krok 1: rozpoznanie BIP

Otwórz `bip_url` w przeglądarce, sprawdź:

1. Czy sesje publikowane są przez **eSesja** (typowe oznaki: URL ze ścieżką `/sesje/` lub iframe z `esesja.pl`, lista głosowań w tabeli z linkami `/glosowania/{id}/`). Jeśli tak: reuse `radoskop/scripts/lib_esesja.py`.
2. Czy BIP jest **statyczny** (HTML generowany serwerowo, brak SPA, transmisje na YouTube, protokoły jako PDF). Jeśli tak: reuse `radoskop/scripts/lib_bip_static.py`.
3. Czy istnieje **API** (rzadko, ale w mazowieckim było coś przy konsultacjach). Jeśli tak: dedykowany skrypt zamiast reuse.

### Krok 2: scaffolding skryptów

Skopiuj wzorzec z dowolnego miasta tej samej rodziny BIP:

```
radoskop/cities/gdansk/scripts/   # eSesja jako wzorzec
radoskop/cities/lublin/scripts/   # BIP statyczny jako wzorzec
```

Tworząc:

- `scrape_glosowania.py`: wynik to `kadencja-{id}.json` z listą sesji, głosowań imiennych, frekwencji.
- `scrape_interpelacje.py`: wynik to `interpelacje.json`. Kontrakt znany z `build_interpelacje_index.py` (akceptuje listę albo dict z `items`).
- `scrape_protokoly.py` (opcjonalnie, jeśli BIP ma osobne protokoły z PDF).

### Krok 3: integracja z pipeline

W `radoskop-premium/scrape_all.sh` dodać sekcję dla `sejmik:{slug}` (lub osobny skrypt `scrape_sejmiki.sh`). W `radoskop-premium/nas/run_pipeline.py` driver iteruje po obu katalogach (cities/ i sejmiki/) wywołując ten sam shellowy entrypoint.

### Krok 4: budowa profili i metryk

Po pierwszym udanym scrape, uruchomić:

- `python3 radoskop/scripts/build_profiles.py --data sejmiki/{slug}/docs/data.json --out sejmiki/{slug}/docs/profiles.json`
- `python3 radoskop/scripts/build_metrics.py sejmiki/{slug}/data/ --out sejmiki/{slug}/docs/data.json`
- `python3 radoskop/scripts/generate_feed.py --base radoskop/sejmiki --city {slug}` (uwaga: parametr `--city` zostaje, choć semantycznie to slug jednostki samorządu).

### Krok 5: deploy i CNAME

- Subdomena `sejmik.{slug}.radoskop.pl` w Cloudflare, CNAME na S3 bucket `radoskop-public` (origin `_sejmik_{slug}/`).
- Skrypt `radoskop-premium/scripts/deploy_main_s3.py` rozszerzony o ścieżkę `_sejmik_{slug}` (TODO).
- Worker Cloudflare dla apex może zostać bez zmian, sejmiki mają własne subdomeny.

### Krok 6: aktualizacja apex

- `generate_main_manifest.py` skanuje `radoskop/sejmiki/` i dorzuca do `cities.json` jeszcze `sejmiki.json` (TODO).
- Statyczna sekcja "Sejmiki województw" w `radoskop/docs/index.html` zostaje zastąpiona dynamiczną siatką po ukończeniu pierwszego pilota.

## Kolejność rekomendowana

1. Mazowieckie pierwsze. Największe województwo, najwięcej radnych (51), największy interes publiczny. Zwykle eSesja, więc reuse `lib_esesja.py`.
2. Dolnośląskie drugie. BIP UMWD jest klasyczny statyczny, sprawdza wzorzec `lib_bip_static.py`.
3. Pomorskie trzecie. Region domowy, dużo wiedzy lokalnej, łatwo zwalidować.

Po tych trzech wzorzec scrape jest stabilny i pozostałe 13 idzie szybciej.

## Czego NIE budujemy w sejmikach

Komisje. Są zarządzane statycznie (jeśli w ogóle), nie skalujemy automatyzacji na to.

Budżet. Wojewódzkie budżety mają inną strukturę niż miejskie (większa rola subwencji centralnej, mniej WPI). Decyzja: zostawiamy `has_budget: false` w config.json, do oceny po pilotażu.
