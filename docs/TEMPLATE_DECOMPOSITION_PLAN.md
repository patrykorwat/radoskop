# Plan dekompozycji `template/index.html` (4796 linii)

Cel: rozbić monolit na utrzymywalne moduły **bez zmiany wygenerowanego outputu**.
Zasada nadrzędna: każdy plasterek weryfikowany testem golden-file (render byte-identyczny
dla wszystkich miast i sejmików). Dopóki hash się zgadza, nic nie zepsute na ~130 stronach.

## Obecna struktura (zmierzone granice)

| Zakres linii | Zawartość | Cel |
|---|---|---|
| 1–66 | `<head>`: meta/SEO, JSON-LD, anti-FOUC theme script, Chart.js CDN | `partials/head.html` |
| 67–566 | jeden `<style>` (~500 linii CSS) | `app/app.css` (token `{{APP_CSS}}`) |
| 568–729 | szkielet `<body>`: topbar, kontener app, loading, tab-bar, kadencja-bar | zostaje w `index.html` (szkielet) |
| 730–4465 | główny `<script>` aplikacji (107 funkcji, ~3735 linii) | `app/js/*.js` (token `{{APP_JS}}`) |
| 4466–4499 | HTML modala logowania | `partials/auth_modal.html` |
| 4500–4794 | drugi `<script>`: auth (login/modal/google) | `app/js/auth.js` (część `{{APP_JS}}` lub osobny `{{AUTH_JS}}`) |

## Mapowanie skryptu na moduły (klastry funkcji, z liniami)

- `js/theme.js` (735–856): `_radoskopCookieDomain`, `radoskopGet/SetCookie`, `getTheme`,
  `applyTheme`, `toggleTheme`. **Współdzielone z landingiem** — docelowo wspólny `partials/theme_toggle.js`.
- `js/votes_categ.js` (857–907): `categorizeVote`, `catPill`, `resolveNV`, `sortByLastName`, `isFemale`.
- `js/app_init.js` (908–1166): `loadKadencja`, `init`, `renderKadencjaBar`, `_applyKadencja`,
  `selectKadencjaQuiet`, `selectKadencja`. Tu jest router init i `navigateTo(mainPath())`.
- `js/router.js` (1167–1324): `activateTab`, `hideAllViews`, `toInternalPath`, `toCanonicalPath`,
  `setTitle`, `setOgMeta`, `mainPath`, `navigateTo`, `routePath`. **Tu żyje LANDING_MODE/mainPath.**
- `js/links.js` (1325–1397): `findProfile`, `profileSlug`, `trackEvent`, `profileLink(Styled)`,
  `voteLink`, `sessionLink(Stop)`.
- `js/static_pages.js` (1398–1483): `showPrivacy`, `showBusiness`, `showTerms`, `showImpressum`,
  `showReports`, `showMain`.
- `js/ranking.js` (1484–1613): `render`, `rosterFromProfiles`, `renderClubFilter`, `initials`,
  `renderCouncilorRoster`, `renderCouncilorTable`, `switchProfileTab`.
- `js/profile.js` (1614–2350): `showProfile` (~736 linii, sam w sobie kandydat na dalszy podział).
- `js/votes.js` (2351–2882): `pctBar`, `getSessionVotes`, `handleVoteSearch/ResultFilter`,
  `renderSessionsTable`, `renderVotesTable`, `isFactionVote`, `factionStance`, `renderFactionVotes`,
  `factionNoticeHtml`, `isShowOfHandsVote`, `showOfHandsNoticeHtml`, `renderShowOfHands`, `showVote`.
- `js/druki.js` (2883–3094): `showDruk`, `loadDrukEntry`, `renderCommissionTimeline`.
- `js/sessions.js` (3095–3396): `backFromVote`, `showSession`.
- `js/komisje.js` (3397–3703): `refreshSubscriptionStatus`, `komisjaSlugFromLabel`,
  `komisjaRoleFromLabel`, `komisjaMembers`, `renderKomisjeList`, `showKomisja`, `backFromKomisja`.
- `js/interpelacje.js` (3704–3863): `renderInterpelacje`.
- `js/budget.js` (3864–4069): `renderBudget`.
- `js/ui.js` (4070–4464): `bindTabs`, `bindSort`, `toggleCompare`, `renderCompareBar`, `clearCompare`,
  `showCompare`, `downloadCard`, `toggleCouncilorAlert`, `_initAlertBtn`, `showEmbedCode`,
  `shareBar(Inner)`, `nativeShare`, `copyShareLink`.
- `js/auth.js` (4517–4794): cały drugi skrypt (`_bridgeLogin` … `_showBanner`).

Kolejność sklejania = kolejność powyżej (zależności: `theme`→`router`→reszta; `auth` na końcu).
To ten sam porządek co dziś w jednym `<script>`, więc semantyka bez zmian.

## Jak składa to generator (krytyczne dla zachowania outputu)

`generate_site.py` dziś: czyta `index.html` → `apply_locale` → `apply_english_paths` →
podstawia `{{TOKEN}}` → zapis. **`apply_locale` i `apply_english_paths` robią `str.replace`
na wzorcach kodu, które żyją w JS** (np. `path.startsWith('/profil/')`). Dlatego:

1. Generator MUSI najpierw skleić pełny HTML (wstrzyknąć `{{APP_CSS}}` i `{{APP_JS}}` z plików),
   a DOPIERO POTEM puścić `apply_locale` / `apply_english_paths` na całości.
2. Tokeny w JS (`{{HAS_VOTING_DATA}}`, `{{LANDING_MODE}}`, `{{CLUB_JS}}`...) zostają w plikach
   `js/*.js` i są podstawiane na sklejonym stringu — bez zmian w słowniku `replacements`.
3. To samo dotyczy `generate_assembly_site.py` (reużywa `index.html`) — czyta ten sam szkielet
   i te same `app/`-pliki, więc dekompozycja działa dla sejmików automatycznie.

Zmiana w generatorze jest minimalna: dodać czytanie `app/app.css` i sklejenie `app/js/*.js`
w nowe tokeny `{{APP_CSS}}` / `{{APP_JS}}` w szkielecie `index.html`. Reszta pipeline bez zmian.

## Siatka bezpieczeństwa (robimy PRZED jakąkolwiek zmianą)

`scripts/snapshot_templates.py`:
- dla każdego `cities/*/config.json` i `assemblies/*/config.json` (nie disabled) renderuje
  `index.html` do tempdir przez odpowiedni generator,
- liczy sha256, zapisuje baseline `docs/_template_snapshots.json`,
- tryb `--check` re-renderuje i porównuje; różnica = czerwony, lista zmienionych miast.

Workflow każdego plasterka: `--check` zielony → wytnij fragment do pliku + token → `--check`
musi dalej być zielony (output bajt w bajt). Jeśli czerwony, fragment się różni → debug zanim dalej.

## Kolejność prac (od najniższego ryzyka)

1. **Snapshot harness** (zero zmian w template, czysty zysk).
2. **CSS → `app/app.css`** + token `{{APP_CSS}}`. Łatwe, duży zysk czytelności, brak logiki.
3. **`partials/head.html` + `partials/auth_modal.html`** (statyczny HTML, niskie ryzyko).
4. **JS: wytnij liść po liściu** w kolejności zależności. Zacznij od izolowanych: `budget.js`,
   `interpelacje.js`, `druki.js`, `votes_categ.js` — mało zależności. Potem `router.js`, `ranking.js`,
   `profile.js`, `votes.js`, `sessions.js`, `komisje.js`, `ui.js`, na końcu `app_init.js`, `auth.js`.
5. **Wspólny motyw** z landingiem: `theme.js` + zmienne CSS jako `partials/` ładowane przez SPA
   (token) i `base.html` (Jinja `{% include %}`). Domyka deduplikację z systemem landingu.

## Ryzyka i pułapki

- **Kolejność locale/english passes** (patrz wyżej) — najczęstsze źródło dryfu. Harness to wyłapie.
- **`<script>` granice**: dziś dwa bloki (app 730–4465, auth 4500–4794). Zachować dwa albo świadomie
  scalić — golden test pokaże, czy scalenie zmienia whitespace (a więc hash). Bezpieczniej: dwa tokeny
  `{{APP_JS}}` i `{{AUTH_JS}}`.
- **Whitespace/indentacja**: sklejanie plików musi odtworzyć dokładne wcięcia, inaczej hash się zmieni
  mimo identycznej semantyki. Opcja: zaakceptować jednorazowy re-baseline po kroku CSS/JS (semantycznie
  identyczny, tylko whitespace), potwierdzony ręcznym diffem + renderem wizualnym kilku miast.
- **`profile.js` (736 linii)** to osobny dług — najpierw wynieść jako całość, podział wewnętrzny później.
- **Brak testów JS w repo** — dodać minimalny smoke (Playwright) na 3 miastach: render `/`, klik taba
  Głosowania, otwarcie profilu, przełącznik motywu. To łapie błędy runtime, których hash nie widzi.

## Efekt końcowy

`template/index.html` kurczy się z 4796 linii do ~200-liniowego szkieletu (head include, body shell,
2 tokeny JS). Edytujesz `app/js/router.js` (≈160 linii) albo `app/app.css` (≈500) zamiast scrollować
monolit. Zero zmian w wygenerowanych stronach na każdym kroku, gwarantowane testem.
