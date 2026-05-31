"""Testy parsera Votebox Landtag MV (scrape_glosowania.parse_votebox_text).

Fixtures to REALNY tekst wyciągnięty z dwóch szablonów PDF Votebox 8. WP:
  * ABSTIMMUNGSPROTOKOLL (117. Sitzung, TOP 35) — nazwiska ze średnikami,
    parser ma je wyciągnąć w całości (names_reliable=True).
  * ABSTIMMUNGSERGEBNIS (98. Sitzung, TOP 32) — układ dwukolumnowy bez
    przecinków; nazwisk nie da się pewnie przypisać do sekcji, więc parser
    MUSI zwrócić poprawne liczniki z nagłówka i PUSTE listy imienne
    (names_reliable=False), zamiast cichych zer (regresja sprzed fixa).

Uruchom: python3 -m pytest assemblies/landtag-mv/scripts/test_parse_votebox.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from scrape_glosowania import (
        extract_section_counts, parse_votebox_text,
        parse_month_pdfs, parse_month_legislation,
        _columns_from_words, _group_words_into_lines,
    )
except Exception as e:  # brak requests/pdfplumber w env — pomiń, nie failuj
    pytest.skip(f"nie można zaimportować scrape_glosowania ({e})", allow_module_level=True)


# ── Szablon ABSTIMMUNGSPROTOKOLL (działa, pełne nazwiska) ────────────────────
PROTOKOLL_117 = """\
LANDTAG MECKLENBURG-VORPOMMERN
117. sitzung - 10.10.2025
ABSTIMMUNGSPROTOKOLL
Antrag der Fraktion der CDU
Aufklärung und Konsequenzen nach Beförderungsskandal im Innenministerium
- Drucksache 8/5297 -
Abgelehnt
Uhrzeit: 10.10.2025 12:34:23 Stimmberechtigte: 79 Stimmen
Abstimmungsart: Offen Abgestimmt: 65 Stimmen
Erforderliche Mehrheit: Einfache Mehrheit 100,00 % 65 Stimmen
Ja 43,08 % 28 Stimmen
Nein 56,92 % 37 Stimmen
Enthaltung 0 Stimmen
Nicht abgestimmt 14 Stimmen
Ja 28 Stimmen
(AfD) de Jesus-Fernandes, Thomas; (AfD) Federau, Petra; (AfD) Förster, Horst; (AfD) Kramer, Nikolaus; (AfD) Meister, Michael; (AfD) Reuken,
Stephan J.; (AfD) Schmidt, Martin; (AfD) Schneider, Jens-Holger; (AfD) Stein, Thore; (AfD) Tadsen, Jan-Phillip; (AfD) Timm, Paul-Joachim;
(BÜNDNIS 90/DIE GRÜNEN) Damm, Hannes; (BÜNDNIS 90/DIE GRÜNEN) Oehlrich, Constanze; (BÜNDNIS 90/DIE GRÜNEN) Terpe (Dr.),
Harald; (BÜNDNIS 90/DIE GRÜNEN) Wegner, Jutta; (CDU) Ehlers, Sebastian; (CDU) Enseleit, Sabine; (CDU) Hoffmeister, Katy; (CDU) Liskow,
Franz-Robert; (CDU) Peters, Daniel; (CDU) Renz, Torsten; (CDU) Schlupp, Beate; (CDU) von Allwörden, Ann Christin; (CDU) Waldmüller,
Wolfgang; (fraktionslos) van Baal, Sandy; (Gruppe der FDP) Becker-Hornickel, Barbara; (Gruppe der FDP) Domke, René; (Gruppe der FDP)
Wulff, David
Nein 37 Stimmen
(Die Linke) Albrecht, Christian; (Die Linke) Bruhn, Dirk; (Die Linke) Koplin, Torsten; (Die Linke) Noetzel, Michael; (Die Linke) Pulz-Debler,
Steffi; (Die Linke) Rösler, Jeannine; (Die Linke) Trepsdorf (Dr.), Daniel; (SPD) Albrecht, Rainer; (SPD) Backhaus (Dr.), Till; (SPD) Barlen, Julian;
(SPD) Beitz, Falko; (SPD) Brade, Christian; (SPD) da Cunha, Philipp; (SPD) Dahlemann, Patrick; (SPD) Drese, Stefanie; (SPD) Falk, Marcel;
(SPD) Gundlack, Tilo; (SPD) Hegenkötter, Beatrix; (SPD) Hesse, Birgit; (SPD) Julitz, Nadine; (SPD) Kaselitz, Dagmar; (SPD) Klingohr, Christine;
(SPD) Krüger, Thomas; (SPD) Martin, Bettina; (SPD) Miraß, Heiko; (SPD) Mucha, Ralf; (SPD) Northoff (Prof. Dr.), Robert; (SPD) Pegel,
Christian; (SPD) Pfeifer, Mandy; (SPD) Rahm-Präger (Dr.), Sylva; (SPD) Schiefler, Michel-Friedrich; (SPD) Schmelzer, Grit; (SPD) Schröder
(Dr.), Anna-Konstanze; (SPD) Stamer, Dirk; (SPD) Winter, Christian; (SPD) Wölk (Dr.), Monique; (SPD) Würdisch, Thomas
Nicht abgestimmt 14 Stimmen
(AfD) Schult, Enrico; (AfD) Schulze-Wiehenbrauk, Jens; (BÜNDNIS 90/DIE GRÜNEN) Shepley, Anne; (CDU) Berg, Christiane; (CDU) Diener,
Thomas; (CDU) Glawe, Harry; (CDU) Reinhardt, Marc; (Die Linke) Foerster, Henning; (Die Linke) Schmidt, Elke-Annette; (fraktionslos)
Schneider-Gärtner (Dr.), Eva Maria; (SPD) Butzki, Andreas; (SPD) Saemann, Nils; (SPD) Schwesig, Manuela; (SPD) Tegtmeier, Martina
Elektronische Abstimmung über Votebox. Infos über Verfahren, Sicherheit und Datenschutz: www.votebox.com. Protokoll erstellt am 10.10.2025. Seite 1/1
"""

# ── Szablon ABSTIMMUNGSERGEBNIS (dwukolumnowy, nazwiska bez przecinków) ──────
ERGEBNIS_98 = """\
Abgelehnt
Stimmberechtigte: 79 Uhrzeit: 31.01.2025 Datum: 15:57:11 Art: Offen
Abgestimmt: 66 Nicht abgestimmt: 13
Einfache Mehrheit der gültigen Stimmen Enthaltung: 0
Ja 43,94% 29 Stimmen
Nein 56,06% 37 Stimmen
LANDTAG MECKLENBURG-VORPOMMERN
98. Sitzung - 31.01.2025
ABSTIMMUNGSERGEBNIS
Antrag der Fraktion der FDP Grundsteuer – Verwerfungen abmildern und gerecht
reformieren - Drucksache 8/4505 -
Nicht abgestimmt 13 Stimmen
1 Stimme
1 Stimme
(SPD) Saemann Nils 1 Stimme
(SPD) Butzki Andreas
(SPD) Hesse Birgit
(SPD) Martin Bettina
Elektronische Abstimmung über Votebox. Infos über Verfahren, Sicherheit und Datenschutz: www.votebox.com. Protokoll erstellt am 31.01.2025. Seite 2/2
"""


def test_counts_protokoll():
    c = extract_section_counts(PROTOKOLL_117)
    assert c["za"] == 28
    assert c["przeciw"] == 37
    assert c["wstrzymal_sie"] == 0
    assert c["brak_glosu"] == 14
    assert c["za"] + c["przeciw"] + c["wstrzymal_sie"] + c["brak_glosu"] == 79


def test_counts_ergebnis():
    c = extract_section_counts(ERGEBNIS_98)
    assert c["za"] == 29
    assert c["przeciw"] == 37
    assert c["wstrzymal_sie"] == 0
    assert c["brak_glosu"] == 13
    assert c["za"] + c["przeciw"] + c["wstrzymal_sie"] + c["brak_glosu"] == 79


def test_protokoll_names_reliable_and_complete():
    p = parse_votebox_text(PROTOKOLL_117)
    assert p["names_reliable"] is True
    nv = p["named_votes"]
    assert len(nv["za"]) == 28
    assert len(nv["przeciw"]) == 37
    assert len(nv["wstrzymal_sie"]) == 0
    assert len(nv["brak_glosu"]) == 14
    # Counts spójne z listami.
    assert p["counts"]["za"] == 28 and p["counts"]["brak_glosu"] == 14


def test_protokoll_name_normalization_and_clubs():
    p = parse_votebox_text(PROTOKOLL_117)
    nv = p["named_votes"]
    # "Nachname, Vorname" -> "Vorname Nachname".
    assert "Thomas de Jesus-Fernandes" in nv["za"]
    assert "Christian Albrecht" in nv["przeciw"]
    # Tytuł akademicki zdjęty: "Terpe (Dr.), Harald" -> "Harald Terpe".
    assert "Harald Terpe" in nv["za"]
    assert "Robert Northoff" in nv["przeciw"]
    # Ostatni wpis nie wciąga stopki ("... Martina\\nElektronische ...").
    assert "Martina Tegtmeier" in nv["brak_glosu"]
    for garbage in nv["za"] + nv["przeciw"] + nv["brak_glosu"]:
        assert "Votebox" not in garbage
        assert "Stimme" not in garbage
    # Klub przypisany.
    assert p["councilor_clubs"]["Thomas de Jesus-Fernandes"] == "AfD"
    assert p["councilor_clubs"]["Harald Terpe"] == "BÜNDNIS 90/DIE GRÜNEN"


def test_ergebnis_counts_kept_names_dropped():
    p = parse_votebox_text(ERGEBNIS_98)
    # Liczniki z nagłówka — poprawne mimo nieparsowalnego układu.
    assert p["counts"]["za"] == 29
    assert p["counts"]["przeciw"] == 37
    assert p["counts"]["brak_glosu"] == 13
    # Listy imienne odrzucone (bezpiecznik), bo nie zgadzają się z licznikami.
    assert p["names_reliable"] is False
    assert all(len(p["named_votes"][cat]) == 0 for cat in p["named_votes"])


def test_ergebnis_no_silent_zeros():
    """Regresja: dawniej ERGEBNIS dawał counts = same zera + 1 fantom."""
    p = parse_votebox_text(ERGEBNIS_98)
    assert sum(p["counts"].values()) == 79
    assert p["topic"]  # temat wyciągnięty
    assert p["drucksache"] == "8/4505"


def test_topic_extraction():
    p = parse_votebox_text(PROTOKOLL_117)
    assert "Beförderungsskandal" in p["topic"]
    assert p["drucksache"] == "8/5297"


# ── Strona miesiąca: linki PDF + wyniki uchwał ──────────────────────────────
MONTH_HTML = """
<html><body>
<h4>Beschlussprotokolle</h4>
<p><a href="https://www.dokumentation.landtag-mv.de/parldok/dokument/66872/8_115_beschlussprotokoll#navpanes=0">Beschlussprotokoll 115. Sitzung</a></p>
<h4>Abgeschlossene Gesetzgebung</h4>
<p>Drs.-Nr. <a href="https://www.dokumentation.landtag-mv.de/parldok/dokument/64429/x#navpanes=0">8/4870</a> angenommen</p>
<p>Drs.-Nr. <a href="https://www.dokumentation.landtag-mv.de/parldok/dokument/65103/y#navpanes=0">8/5119</a> abgelehnt</p>
<p>Drs.-Nr. <a href="https://www.dokumentation.landtag-mv.de/parldok/dokument/9999/z#navpanes=0">8/5119</a> abgelehnt</p>
<h4>Namentliche Abstimmungen</h4>
<a href="https://www.landtag-mv.de/fileadmin/1.Abteilung_P/PD1/Namentliche_Abstimmungen/2025-10-10-117._Sitzung_Namentliche_Abstimmung_zu_TOP_35.pdf">117. Sitzung TOP 35</a>
</body></html>
"""


def test_month_pdfs_parsing():
    urls = parse_month_pdfs(MONTH_HTML)
    assert len(urls) == 1
    assert urls[0].endswith("Namentliche_Abstimmung_zu_TOP_35.pdf")


def test_month_legislation_parsing():
    legs = parse_month_legislation(MONTH_HTML)
    keys = {(l["drucksache"], l["result"]) for l in legs}
    # Wyniki wyciągnięte, Beschlussprotokoll (bez słowa-wyniku) pominięty.
    assert ("8/4870", "angenommen") in keys
    assert ("8/5119", "abgelehnt") in keys
    # Dedup po (drs, wynik): druga 8/5119 abgelehnt nie dubluje.
    assert len(legs) == 2
    # URL bez kotwicy #navpanes.
    assert all("#" not in l["url"] for l in legs)


# ── Rekonstrukcja kolumn (ratunek dla dwukolumnowego ERGEBNIS) ──────────────
def _w(text, x0, top):
    return {"text": text, "x0": x0, "x1": x0 + 20, "top": top, "bottom": top + 10}


def test_group_words_into_lines_order():
    words = [_w("b", 80, 100), _w("a", 40, 100), _w("c", 40, 120)]
    assert _group_words_into_lines(words) == ["a b", "c"]


def test_columns_left_fully_before_right():
    # Strona 600px: środek 300. Lewa kolumna (x0~40/65), prawa (x0~300/325).
    words = [
        _w("La", 40, 100), _w("Lb", 65, 100),
        _w("Lc", 40, 120), _w("Ld", 65, 120),
        _w("Le", 40, 140), _w("Lf", 65, 140),
        _w("Ra", 300, 100), _w("Rb", 325, 100),
        _w("Rc", 300, 120), _w("Rd", 325, 120),
        _w("Re", 300, 140), _w("Rf", 325, 140),
    ]
    text = _columns_from_words(words, page_width=600)
    assert text.split("\n") == ["La Lb", "Lc Ld", "Le Lf", "Ra Rb", "Rc Rd", "Re Rf"]
