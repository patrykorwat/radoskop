/**
 * Test kontraktu danych: front (template/index.html) <-> helper
 * (scripts/lib_faction_votes.py).
 *
 * Regiony bez głosowań imiennych (Francja, część gremiów DE) renderują się
 * przez funkcje JS isFactionVote / factionStance / renderFactionVotes /
 * factionNoticeHtml. Te funkcje konsumują rekord zbudowany w Pythonie przez
 * make_faction_vote(). Ten test wyciąga PRAWDZIWE funkcje z template i puszcza
 * je na PRAWDZIWYM wyjściu helpera, więc każdy rozjazd kontraktu (zmiana nazwy
 * kategorii, klasy stance, pola seats) wywali test, zanim trafi na produkcję.
 *
 * Uruchomienie:
 *   node scripts/verify_faction_frontend.mjs <vote.json>
 * gdzie vote.json to wyjście make_faction_vote() zrzucone przez Pythona.
 * Bez argumentu czyta JSON ze stdin.
 *
 * Exit 0 = kontrakt zgodny. Exit 1 = rozjazd (z opisem).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TEMPLATE = join(__dirname, "..", "template", "index.html");

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}
function assert(cond, msg) {
  if (!cond) fail(msg);
}

// 1. Wyciągnij blok funkcji frakcyjnych z template (od isFactionVote do
//    początku showVote). Slice na żywym pliku, żeby test śledził źródło.
const html = readFileSync(TEMPLATE, "utf8");
const start = html.indexOf("function isFactionVote(vote) {");
const end = html.indexOf("async function showVote(");
assert(start !== -1, "nie znaleziono function isFactionVote w template");
assert(end !== -1 && end > start, "nie znaleziono async function showVote w template");
const jsBlock = html.slice(start, end);
for (const fn of ["isFactionVote", "factionStance", "renderFactionVotes", "factionNoticeHtml"]) {
  assert(jsBlock.includes("function " + fn), "brak funkcji " + fn + " w wyciętym bloku");
}

// 2. Stub clubColor (na produkcji bierze kolor z config.clubs).
const clubColor = (code) => "#123456:" + code;

// 3. Załaduj funkcje z template do bieżącego scope.
const loaded = new Function(
  "clubColor",
  jsBlock + "\nreturn { isFactionVote, factionStance, renderFactionVotes, factionNoticeHtml };"
)(clubColor);
const { isFactionVote, factionStance, renderFactionVotes, factionNoticeHtml } = loaded;

// 4. Wczytaj rekord z helpera Pythona.
const src = process.argv[2]
  ? readFileSync(process.argv[2], "utf8")
  : readFileSync(0, "utf8");
const vote = JSON.parse(src);

// 5. Asercje kontraktu.
assert(isFactionVote(vote) === true, "isFactionVote powinno być true dla vote_mode=faction");

// factionStance — jednostkowo na syntetycznych grupach.
assert(factionStance({ za: 10, przeciw: 0, wstrzymal_sie: 0 }).key === "za", "dominujące za");
assert(factionStance({ za: 0, przeciw: 7, wstrzymal_sie: 0 }).key === "przeciw", "dominujące przeciw");
assert(factionStance({ za: 3, przeciw: 3, wstrzymal_sie: 0 }).key === "mixed", "remis -> mixed");
assert(factionStance({ za: 0, przeciw: 0, wstrzymal_sie: 0, nieobecni: 5 }).key === "none", "sami nieobecni -> none");

// factionStance na realnych grupach z rekordu — każda musi dać znaną klasę.
const known = new Set(["za", "przeciw", "wstrzymal_sie", "mixed", "none"]);
for (const [code, g] of Object.entries(vote.faction_votes)) {
  const st = factionStance(g);
  assert(known.has(st.key), `nieznany stance key '${st.key}' dla grupy ${code}`);
  assert(typeof st.cls === "string" && st.cls.startsWith("stance-"), `zła klasa stance dla ${code}`);
}

// renderFactionVotes — HTML musi zawierać nazwy grup, nagłówek i legendę.
const rendered = renderFactionVotes(vote);
assert(rendered.includes("Głosowanie według frakcji"), "brak nagłówka sekcji frakcyjnej");
assert(rendered.includes("faction-rows"), "brak kontenera faction-rows");
assert(rendered.includes("faction-legend"), "brak legendy");
for (const code of Object.keys(vote.faction_votes)) {
  assert(rendered.includes(code), `render nie zawiera kodu frakcji ${code}`);
}
// swatch powinien użyć clubColor (stub zostawia rozpoznawalny ślad).
assert(rendered.includes("#123456:"), "renderFactionVotes nie wywołał clubColor dla swatcha");
// etykieta mandatów (l.poj./mn.) powinna się pojawić, bo seats są w rekordzie.
assert(/mandat(ów)?/.test(rendered), "brak etykiety mandatów");

// factionNoticeHtml — notka wyjaśniająca brak głosowań imiennych.
const notice = factionNoticeHtml();
assert(notice.includes("głosowania imienne"), "notka nie wyjaśnia braku głosowań imiennych");
assert(notice.includes("faction-notice"), "notka bez klasy faction-notice");

// Wypisz render na stdout — używany dalej do testu lokalizacji (de/fr).
process.stdout.write(JSON.stringify({ ok: true, rendered, notice }));
