"""Testy parsera agendy Conseil de Paris (scrape_paris).

Fixtures to REALNE rekordy z żywego API opendata.paris.fr (Explore v2.1,
dataset ordre-du-jour-du-conseil-de-paris-...), pobrane 2026-05-24.

Uruchom: python3 -m pytest cities/paris/scripts/test_scrape_paris.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from scrape_paris import (
        agenda_records_to_sessions,
        build_faction_vote_from_tableau,
        build_votes_from_pv_results,
        discover_pv_urls,
        extract_session_date,
        parse_annex_text,
        parse_compte_rendu_sommaire,
        parse_scrutins,
        parse_seance_date,
    )
except Exception as e:
    pytest.skip(f"nie można zaimportować scrape_paris ({e})", allow_module_level=True)


# Realny fragment OCR aneksu (scrutin public) — wiersze per radny z zawijaniem
# nazwy grupy do drugiej linii. Choix: Pour[++]/Contre[+]/Abst[-]/NPPV[--].
ANNEX_OCR = """\
ID du siégTitre Nom Groupe Procuration par Choix de vote Pondération
32 AHOUDIAN Adji 1 - Socialiste et Divers Contre[+] 1
Gauche
65 ARENAS Rodrigo 4 - Nouveau Paris Pour[++] 1
Populaire
68 DUTREUIL Frédérique 2 - Ecologiste et Social de Abst[-] 1
Paris
133 ALPHAND David 5 - Paris Liberté NPPV[--] 1
146 BOUADMA Lila 8 - Non-inscrits Contre[+] 1
"""

# Realny blok scrutin public z compte rendu (séance 14.04.2026, vœu n° 5).
SCRUTIN_TEXT = """\
Scrutin public sur le vœu n° 5.
Le scrutin est ouvert.
Le scrutin est clos.
Les résultats sont les suivants :
Nombre d'inscrits : 163
Nombre de votants : 143
Abstentions : 6
NPPV : 29
Pour : 12
Contre : 96
(Voir détail des votes annexe n° 1).
Le vœu n° 5, avec un avis défavorable de l'Exécutif, est rejeté au scrutin public.
"""


# Realne formaty linków ze strony comptes rendus (cdn.paris.fr).
COMPTES_RENDUS_HTML = """
<a href="https://cdn.paris.fr/paris/2025/10/17/compte_rendu_sommaire_octobre_25-nCAe.pdf">Octobre 2025</a>
<a href="https://cdn.paris.fr/paris/2025/06/13/sommaire-juin-2025-5mnl.pdf">Juin 2025</a>
<a href="https://cdn.paris.fr/paris/2026/04/22/sommaire_avril_2026-compresse-QySk.pdf">Avril 2026</a>
<a href="https://cdn.paris.fr/paris/2025/02/26/proces-verbal-integral-fevrier-2025.pdf">PV intégral (pas sommaire)</a>
"""


# Realny fragment compte rendu sommaire (séance 7-10 października 2025).
PV_SOMMAIRE = """\
2025 DSP 14 Subvention (88.000 euros) à l'association Le Bus des Femmes (20e).
Le projet de délibération DSP 14 est adopté à main levée.
2025 DSP 72 Subvention de la ville pour l'organisation d'actions de dépistage et
d’éducation bucco-dentaires réalisées par le pôle prévention bucco-dentaire de la
caisse primaire d’assurance maladie de Paris.
Le projet de délibération DSP 72 est adopté à main levée.
Vœu n° 16 déposé par le groupe Paris en commun relatif à l’Aide Médicale d’État et à
l’accès aux soins pour toutes et tous.
Le vœu n° 16, avec un avis favorable de l'Exécutif, est adopté à main levée.
Vœu n° 17 déposé par le groupe Changer Paris relatif à la présence constatée de
substances perfluoroalkylées dans l’air des environs de l’incinérateur d’Ivry-Paris.
Vœu n° 17 bis déposé par l’Exécutif.
Le vœu n° 17, est retiré.
Le vœu n° 17 bis déposé par l'Exécutif, est adopté, à l’unanimité, à main levée.
"""


# Realne rekordy z API (pole po polu jak zwraca Opendatasoft).
REAL_RECORDS = [
    {
        "seance": "du lundi 15 février 2016 au mardi 16 février 2016",
        "reference": "V14 .",
        "entite_depositaire": "LR",
        "elu_depositaire": "Florence BERTHOUT",
        "objet": "relatif aux balayeurs supplémentaires.",
        "type": "V",
        "rapporteur": None,
    },
    {
        "seance": "du lundi 13 juin 2016 au mardi 14 juin 2016",
        "reference": "2016 DFPE 218",
        "entite_depositaire": None,
        "elu_depositaire": None,
        "objet": "Crèche 11, rue Villiot (12e) - Indemnisation amiable de la MAPA.",
        "type": "PJ",
        "rapporteur": "Mme Nawel OUMER (4ème Commission) rapporteure.",
    },
]


def test_parse_seance_date_range_takes_start():
    assert parse_seance_date("du lundi 15 février 2016 au mardi 16 février 2016") == "2016-02-15"
    assert parse_seance_date("du lundi 13 juin 2016 au mardi 14 juin 2016") == "2016-06-13"


def test_parse_seance_date_single_and_accents():
    assert parse_seance_date("le mardi 5 mars 2024") == "2024-03-05"
    assert parse_seance_date("séance du 1 décembre 2025") == "2025-12-01"


def test_parse_seance_date_garbage():
    assert parse_seance_date(None) is None
    assert parse_seance_date("") is None
    assert parse_seance_date("pas de date") is None


def test_agenda_grouping_and_sort():
    sessions = agenda_records_to_sessions(REAL_RECORDS)
    assert len(sessions) == 2
    # Sort malejąco po dacie: czerwiec 2016 przed lutym 2016.
    assert sessions[0]["date"] == "2016-06-13"
    assert sessions[1]["date"] == "2016-02-15"
    assert sessions[0]["item_count"] == 1


def test_agenda_item_fields():
    sessions = agenda_records_to_sessions(REAL_RECORDS)
    feb = [s for s in sessions if s["date"] == "2016-02-15"][0]
    item = feb["items"][0]
    assert item["type"] == "V"
    assert item["type_label"] == "Vœu"
    assert item["group"] == "LR"
    assert item["elu"] == "Florence BERTHOUT"
    assert item["reference"] == "V14 ."


def _by_ref(results, ref):
    hits = [r for r in results if r["reference"] == ref]
    assert hits, f"brak wyniku dla {ref} w {[r['reference'] for r in results]}"
    return hits[0]


def test_pv_extracts_pj_results():
    res = parse_compte_rendu_sommaire(PV_SOMMAIRE)
    dsp14 = _by_ref(res, "DSP 14")
    assert dsp14["result"] == "adopté"
    assert dsp14["passed"] is True
    assert dsp14["modalite"] == "main levée"
    dsp72 = _by_ref(res, "DSP 72")
    assert dsp72["result"] == "adopté"
    assert "dépistage" in (dsp72["topic"] or "")


def test_pv_extracts_voeu_results_and_groups():
    res = parse_compte_rendu_sommaire(PV_SOMMAIRE)
    v16 = _by_ref(res, "n° 16")
    assert v16["result"] == "adopté"
    assert v16["deposited_by"] == "Paris en commun"
    assert v16["group_code"] == "PARIS_EN_COMMUN"
    assert "Aide Médicale" in (v16["topic"] or "")
    v17 = _by_ref(res, "n° 17")
    assert v17["result"] == "retiré"
    assert v17["passed"] is None
    assert v17["group_code"] == "CHANGER_PARIS"


def test_pv_unanimite_flag():
    res = parse_compte_rendu_sommaire(PV_SOMMAIRE)
    v17bis = _by_ref(res, "n° 17 bis")
    assert v17bis["result"] == "adopté"
    assert v17bis["unanimite"] is True


def test_extract_session_date():
    hdr = "Ville de Paris ►Conseil de Paris ►Séance des 7, 8, 9 et 10 octobre 2025 ►Compte rendu sommaire"
    assert extract_session_date(hdr) == "2025-10-07"


def test_build_votes_show_of_hands():
    results = parse_compte_rendu_sommaire(PV_SOMMAIRE)
    votes = build_votes_from_pv_results(results, "2025-10-07")
    assert len(votes) == len(results)
    v = votes[0]
    assert v["vote_mode"] == "show_of_hands"
    assert v["session_date"] == "2025-10-07"
    assert v["id"].startswith("paris_2025-10-07_")
    # à main levée: brak liczb, puste listy imienne i frakcyjne.
    assert sum(v["counts"].values()) == 0
    assert v["faction_votes"] == {}
    assert all(v["named_votes"][k] == [] for k in v["named_votes"])
    # Wynik zachowany.
    assert v["result"] in {"adopté", "rejeté", "retiré", "ajourné"}


def test_build_votes_voeu_carries_group():
    results = parse_compte_rendu_sommaire(PV_SOMMAIRE)
    votes = build_votes_from_pv_results(results, "2025-10-07")
    v16 = [v for v in votes if v["reference"] == "n° 16"][0]
    assert v16["deposited_by_code"] == "PARIS_EN_COMMUN"
    assert v16["passed"] is True


def test_discover_pv_urls_only_sommaire():
    urls = discover_pv_urls(COMPTES_RENDUS_HTML)
    assert len(urls) == 3
    assert all("sommaire" in u.lower() for u in urls)
    assert "compte_rendu_sommaire_octobre_25-nCAe.pdf" in urls[0]
    # PV intégral (bez 'sommaire') pominięty.
    assert not any("integral" in u for u in urls)


def test_parse_annex_text_groups_and_choices():
    p = parse_annex_text(ANNEX_OCR)
    c = p["counts"]
    assert c["za"] == 1 and c["przeciw"] == 2 and c["wstrzymal_sie"] == 1 and c["brak_glosu"] == 1
    fv = p["faction_votes"]
    # kody kanoniczne (stabilne mimo truncacji nazwy w OCR)
    assert fv["SOCIALISTE_DG"]["przeciw"] == 1
    assert fv["NOUVEAU_PARIS"]["za"] == 1
    assert fv["ECOLOGISTE"]["wstrzymal_sie"] == 1
    assert fv["PARIS_LIBERTE"]["brak_glosu"] == 1
    assert fv["NZ"]["przeciw"] == 1
    # nazwiska per kategoria
    assert "ARENAS Rodrigo" in p["named_votes"]["za"]
    assert "AHOUDIAN Adji" in p["named_votes"]["przeciw"]
    assert "DUTREUIL Frédérique" in p["named_votes"]["wstrzymal_sie"]


def test_parse_scrutins_aggregate_counts():
    s = parse_scrutins(SCRUTIN_TEXT)
    assert ("voeu", "5") in s
    c = s[("voeu", "5")]
    assert c["za"] == 12 and c["przeciw"] == 96
    assert c["wstrzymal_sie"] == 6 and c["brak_glosu"] == 29
    assert c["nieobecni"] == 163 - 143  # inscrits - votants


def test_scrutin_result_uses_counts_and_passed():
    res = parse_compte_rendu_sommaire(SCRUTIN_TEXT)
    v5 = [r for r in res if r["reference"] == "n° 5"][0]
    assert v5["scrutin"] is True
    assert v5["counts"]["za"] == 12 and v5["counts"]["przeciw"] == 96
    assert v5["passed"] is False  # 12 < 96
    votes = build_votes_from_pv_results(res, "2026-04-14")
    bv = [v for v in votes if v["reference"] == "n° 5"][0]
    assert bv["vote_mode"] == "scrutin"  # bez OCR aneksu -> tylko zbiorcze
    assert bv["counts"]["przeciw"] == 96


def test_repousse_is_rejected():
    res = parse_compte_rendu_sommaire(
        "Vœu n° 7 déposé par le groupe Paris Liberté ! relatif à X.\n"
        "Le vœu n° 7, avec un avis défavorable de l'Exécutif, est repoussé.\n"
    )
    v7 = [r for r in res if r["reference"] == "n° 7"][0]
    assert v7["result"] == "rejeté"
    assert v7["passed"] is False


def test_faction_vote_adapter_shape():
    """Adapter PV -> rekord frakcyjny daje kontrakt zgodny z frontem."""
    v = build_faction_vote_from_tableau(
        vote_id="paris_2024_x",
        session_date="2024-03-15",
        topic="Budget primitif 2024",
        tableau={
            "PARIS_EN_COMMUN": {"za": 90, "przeciw": 0, "wstrzymal_sie": 2},
            "CHANGER_PARIS": {"za": 0, "przeciw": 22, "wstrzymal_sie": 0},
        },
    )
    assert v["vote_mode"] == "faction"
    assert v["counts"]["za"] == 90
    assert v["counts"]["przeciw"] == 22
    assert set(v["faction_votes"]) == {"PARIS_EN_COMMUN", "CHANGER_PARIS"}
    # Brak list imiennych (Paryż nie ma głosów per radny).
    assert all(v["named_votes"][k] == [] for k in v["named_votes"])
