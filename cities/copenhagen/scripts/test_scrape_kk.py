"""Smoke testy parsera scrape_kk.py. Bez sieci."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

CITY_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = CITY_DIR.parents[1]
sys.path.insert(0, str(CITY_DIR / "scripts"))
sys.path.insert(0, str(REPO_DIR / "scripts"))

import scrape_kk  # noqa: E402


def _config() -> dict:
    return json.loads((CITY_DIR / "config.json").read_text(encoding="utf-8"))


# Realne teksty z kk.dk (2025), uproszczone do treści sekcji Beslutning.
BESLUTNING_FACTION_SPLIT = """
Beslutning Borgerrepræsentationens beslutning i mødet den 2. oktober 2025
Indstillingen blev godkendt med 42 stemmer mod 4. 8 medlemmer undlod at stemme.
For stemte: Ø (Charlotte Lund, Hassan Nur Wardere, Knud Holt Nielsen, Maria Sofie Petersen), A, C, F, B, V, Å, I, O og Helle Jønch
Imod stemte: Ø (Absalon Billehøj, Frederik W. Kronborg, Gyda Heding, Klaus Goldschmidt Henriksen)
Undlod at stemme: Ø (Bente Møller, Gorm Anker Gunnarsen, Karina Vestergård Madsen, Katrine Hassenkam, Line Barfod, Mikkel Skovgaard, Stine Finné Toft) og Troels Christian Jakobsen
Bilag
"""

BESLUTNING_FACTION_BLOCK = """
Beslutning Borgerrepræsentationens beslutning i mødet den 22. maj 2025
Medlemsforslaget blev vedtaget med 43 stemmer imod 7. Ingen medlemmer undlod at stemme.
For stemte: Ø, A, C, F, B, Å og Finn Rudaizky (Løsgænger)
Imod stemte: V, I og Helle Jønch (Løsgænger)
Bilag
"""

BESLUTNING_UDEN = """
Beslutning Borgerrepræsentationens beslutning i mødet den 22. maj 2025
Indstillingen blev godkendt uden afstemning.
Bilag
"""

BESLUTNING_UDEN_MED_PROTOKOL = """
Beslutning Borgerrepræsentationens beslutning i mødet den 22. maj 2025
Indstillingen blev godkendt uden afstemning. SF videreførte følgende protokolbemærkning:
"Det er ikke muligt..." Bilag
"""


class ParseBeslutning(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _config()

    def test_uden_afstemning_pure(self) -> None:
        d = scrape_kk._parse_beslutning_block(BESLUTNING_UDEN, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "show_of_hands")
        self.assertTrue(d["passed"])
        self.assertEqual(d["modalite"], "uden_afstemning")

    def test_uden_afstemning_with_protokolbemaerkning(self) -> None:
        d = scrape_kk._parse_beslutning_block(BESLUTNING_UDEN_MED_PROTOKOL, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "show_of_hands")

    def test_faction_block_vote(self) -> None:
        d = scrape_kk._parse_beslutning_block(BESLUTNING_FACTION_BLOCK, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "faction")
        self.assertTrue(d["passed"])
        self.assertEqual(d["counts"]["za"], 43)
        self.assertEqual(d["counts"]["przeciw"], 7)
        self.assertEqual(d["counts"]["wstrzymal_sie"], 0)
        # Wszystkie bookstaver bez nawiasów dostały seats z config.
        fv = d["faction_votes"]
        self.assertIn("OE", fv)  # Ø
        self.assertEqual(fv["OE"]["za"], 15)
        self.assertEqual(fv["A"]["za"], 10)
        self.assertEqual(fv["C"]["za"], 8)
        # Niezrzeszeni -> NZ (Finn Rudaizky za, Helle Jønch przeciw)
        self.assertIn("NZ", fv)
        self.assertEqual(fv["NZ"]["za"], 1)
        self.assertEqual(fv["NZ"]["przeciw"], 1)

    def test_faction_with_party_split(self) -> None:
        d = scrape_kk._parse_beslutning_block(BESLUTNING_FACTION_SPLIT, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "faction")
        self.assertEqual(d["counts"]["za"], 42)
        self.assertEqual(d["counts"]["przeciw"], 4)
        self.assertEqual(d["counts"]["wstrzymal_sie"], 8)
        fv = d["faction_votes"]
        # Ø ma rozłam: 4 za + 4 przeciw + 7 undlod = 15 z 15 mandatów
        oe = fv["OE"]
        self.assertEqual(oe["za"], 4)
        self.assertEqual(oe["przeciw"], 4)
        self.assertEqual(oe["wstrzymal_sie"], 7)
        # Pozostali bookstavowie bez rozłamu: cały klub za.
        self.assertEqual(fv["A"]["za"], 10)
        self.assertEqual(fv["C"]["za"], 8)
        # Helle Jønch (Løsgænger) -> NZ za
        self.assertEqual(fv["NZ"]["za"], 1)
        # Troels Christian Jakobsen (bez "Løsgænger") wpada do NZ undlod.
        self.assertEqual(fv["NZ"]["wstrzymal_sie"], 1)
        # named_votes powinien zawierać rozłam.
        nv = d["named_votes"]
        names_za = " ".join(nv["za"])
        self.assertIn("Charlotte Lund", names_za)
        self.assertIn("Helle Jønch", names_za)

    def test_no_beslutning_returns_none(self) -> None:
        d = scrape_kk._parse_beslutning_block("Foo bar baz, nic tu nie ma.", self.cfg)
        self.assertIsNone(d)

    def test_uden_afstemning_trukket_tilbage_is_neutral(self) -> None:
        text = "Beslutning Borgerrepræsentationens beslutning ... Indstillingen blev trukket tilbage uden afstemning."
        d = scrape_kk._parse_beslutning_block(text, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "show_of_hands")
        self.assertIsNone(d["passed"])  # neutralne, nie False

    def test_uden_afstemning_udsat_is_neutral(self) -> None:
        text = "Beslutning Borgerrepræsentationens beslutning ... Sagen blev udsat uden afstemning."
        d = scrape_kk._parse_beslutning_block(text, self.cfg)
        self.assertIsNotNone(d)
        self.assertIsNone(d["passed"])

    def test_uden_afstemning_forkastet(self) -> None:
        text = "Beslutning Borgerrepræsentationens beslutning ... Medlemsforslaget blev forkastet uden afstemning."
        d = scrape_kk._parse_beslutning_block(text, self.cfg)
        self.assertIsNotNone(d)
        self.assertFalse(d["passed"])

    def test_uden_afstemning_subject_variant_borgerrep(self) -> None:
        text = "Beslutning Borgerrepræsentationens beslutning ... Borgerrepræsentationen vedtog indstillingen uden afstemning."
        d = scrape_kk._parse_beslutning_block(text, self.cfg)
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "show_of_hands")
        self.assertTrue(d["passed"])

    def test_uden_afstemning_subject_variant_aendring(self) -> None:
        text = "Beslutning ... Ændringsforslaget blev godkendt uden afstemning."
        d = scrape_kk._parse_beslutning_block(text, self.cfg)
        self.assertIsNotNone(d)
        self.assertTrue(d["passed"])


class ParsePunkt(unittest.TestCase):
    """Lekkie integracyjne testy parse_punkt na zlepkach HTML."""

    def setUp(self) -> None:
        self.cfg = _config()

    def test_parse_punkt_faction(self) -> None:
        html = (
            '<html><head><title>Kirkeligning 2026 | Københavns Kommune</title></head>'
            '<body><h1>Kirkeligning 2026</h1>'
            '<p>Sagsfremstilling lorem ipsum.</p>'
            '<h2>Beslutning</h2>'
            '<p>Borgerrepræsentationens beslutning i mødet den 2. oktober 2025</p>'
            '<p>Indstillingen blev godkendt med 42 stemmer mod 4. 8 medlemmer undlod at stemme.</p>'
            '<p>For stemte: Ø, A, C, F, B, V, Å, I, O og Helle Jønch</p>'
            '<p>Imod stemte: Ø (Absalon Billehøj, Frederik W. Kronborg, Gyda Heding, Klaus Goldschmidt Henriksen)</p>'
            '<p>Undlod at stemme: Ø (Bente Møller, Gorm Anker Gunnarsen, Karina Vestergård Madsen, Katrine Hassenkam, Line Barfod, Mikkel Skovgaard, Stine Finné Toft) og Troels Christian Jakobsen</p>'
            '<h2>Bilag</h2></body></html>'
        )
        url = "https://www.kk.dk/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-02102025/referat/punkt-3"
        v = scrape_kk.parse_punkt(url, html, self.cfg)
        self.assertIsNotNone(v)
        self.assertEqual(v["vote_mode"], "faction")
        self.assertEqual(v["session_date"], "2025-10-02")
        self.assertEqual(v["punkt"], 3)
        self.assertIn("Kirkeligning", v["topic"])
        self.assertEqual(v["counts"]["za"], 42)

    def test_parse_punkt_uden_afstemning(self) -> None:
        html = (
            '<h1>Tilladelse til X</h1>'
            '<h2>Beslutning</h2>'
            '<p>Borgerrepræsentationens beslutning i mødet den 22. maj 2025</p>'
            '<p>Indstillingen blev godkendt uden afstemning.</p>'
        )
        url = "https://www.kk.dk/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat/punkt-7"
        v = scrape_kk.parse_punkt(url, html, self.cfg)
        self.assertIsNotNone(v)
        self.assertEqual(v["vote_mode"], "show_of_hands")
        self.assertTrue(v["passed"])
        self.assertEqual(v["session_date"], "2025-05-22")
        self.assertEqual(v["punkt"], 7)


class UdvalgParsing(unittest.TestCase):
    def test_single_medlem_af(self) -> None:
        u = scrape_kk._parse_udvalg("Medlem af Økonomiudvalget")
        self.assertEqual(u, ["Økonomiudvalget"])

    def test_medlem_af_og_keeps_compound_name(self) -> None:
        # Sundheds- og Omsorgsudvalget ZAWIERA " og " w nazwie — nie wolno
        # splittować po " og " bo rozbije nazwę na pseudo-udvalgi.
        u = scrape_kk._parse_udvalg("Medlem af Socialudvalget og Sundheds- og Omsorgsudvalget")
        self.assertEqual(u, ["Socialudvalget", "Sundheds- og Omsorgsudvalget"])

    def test_borgmester_forperson(self) -> None:
        u = scrape_kk._parse_udvalg(
            "Børne- og ungdomsborgmesteren er forperson for Børne- og Ungdomsudvalget."
        )
        self.assertEqual(u, ["Børne- og Ungdomsudvalget (forperson)"])

    def test_borgmester_title_fallback(self) -> None:
        # Wpis "Line Barfod, klima-, miljø- og teknikborgmester" gdy
        # after_text nie ma "X-borgmesteren er forperson for Y".
        self.assertEqual(
            scrape_kk._udvalg_from_borgmester_title("klima-, miljø- og teknikborgmester"),
            "Klima-, Miljø- og Teknikudvalget",
        )
        self.assertEqual(
            scrape_kk._udvalg_from_borgmester_title("overborgmester"),
            "Økonomiudvalget",
        )
        self.assertEqual(
            scrape_kk._udvalg_from_borgmester_title("sundheds- og omsorgsborgmester"),
            "Sundheds- og Omsorgsudvalget",
        )

    def test_no_udvalg(self) -> None:
        self.assertEqual(scrape_kk._parse_udvalg(""), [])
        self.assertEqual(scrape_kk._parse_udvalg("Random text without udvalg"), [])


class IndexParsing(unittest.TestCase):
    def test_punkt_link_discovery(self) -> None:
        html = (
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat/punkt-1">P1</a> '
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat/punkt-33">P33</a> '
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat/punkt-7">P7</a>'
        )
        out = scrape_kk.discover_punkt_urls(html)
        self.assertEqual([n for _, n in out], [1, 7, 33])

    def test_meeting_link_discovery(self) -> None:
        html = (
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat">A</a> '
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-22052025/referat/punkt-1">B</a> '
            '<a href="/dagsordener-og-referater/Borgerrepr%C3%A6sentationen/m%C3%B8de-30042025/referat">C</a>'
        )
        # discover_meeting_urls robi sieć — używamy MEETING_LINK_RE bezpośrednio.
        urls = []
        for m in scrape_kk.MEETING_LINK_RE.finditer(html):
            urls.append((scrape_kk.BASE + m.group(0), m.group(1)))
        # Dwa unikalne posiedzenia (22052025 i 30042025), nie liczymy punktu.
        dates = sorted({d for _, d in urls})
        self.assertEqual(dates, ["22052025", "30042025"])


class DateMapping(unittest.TestCase):
    def test_iso_conversion(self) -> None:
        self.assertEqual(scrape_kk._ddmmyyyy_to_iso("22052025"), "2025-05-22")
        self.assertEqual(scrape_kk._ddmmyyyy_to_iso("01012026"), "2026-01-01")
        self.assertEqual(scrape_kk._ddmmyyyy_to_iso("bad"), "")

    def test_kadencja_mapping(self) -> None:
        cfg = _config()
        self.assertEqual(scrape_kk._kadencja_for_date("2025-10-02", cfg), "2022-2025")
        self.assertEqual(scrape_kk._kadencja_for_date("2026-02-26", cfg), "2026-2029")
        # data poza zdefiniowanymi kadencjami -> kadencja_active
        self.assertEqual(scrape_kk._kadencja_for_date("2014-06-01", cfg), "2026-2029")


if __name__ == "__main__":
    unittest.main()
