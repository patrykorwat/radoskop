"""Testy dla lib_faction_votes oraz rejestracji locale FR w i18n."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import lib_faction_votes as fv  # noqa: E402
import i18n  # noqa: E402


# ── faction_votes_from_named ─────────────────────────────────────────

def test_aggregate_from_names():
    name_to_club = {
        "Anna": "CDU", "Bernd": "CDU", "Clara": "SPD",
        "Dieter": "SPD", "Eva": "GRUENE",
    }
    named = {
        "za": ["Anna", "Bernd", "Clara"],
        "przeciw": ["Dieter"],
        "wstrzymal_sie": ["Eva"],
        "brak_glosu": [],
        "nieobecni": [],
    }
    resolve = fv.resolver_from_name_map(name_to_club)
    out = fv.faction_votes_from_named(named, resolve)
    assert out["CDU"]["za"] == 2
    assert out["SPD"]["za"] == 1 and out["SPD"]["przeciw"] == 1
    assert out["GRUENE"]["wstrzymal_sie"] == 1


def test_aggregate_unknown_falls_back_to_NZ():
    resolve = fv.resolver_from_name_map({"Anna": "CDU"})
    out = fv.faction_votes_from_named({"za": ["Anna", "Ghost"]}, resolve)
    assert out["CDU"]["za"] == 1
    assert out[fv.UNKNOWN_CLUB]["za"] == 1


def test_aggregate_seats_attached():
    resolve = fv.resolver_from_name_map({"Anna": "CDU"})
    out = fv.faction_votes_from_named(
        {"za": ["Anna"]}, resolve, club_seats={"CDU": 52}
    )
    assert out["CDU"]["seats"] == 52


def test_resolver_from_councilors_by_index():
    councilors = [{"name": "X", "club": "SPD"}, {"name": "Y", "club": "CDU"}]
    resolve = fv.resolver_from_councilors(councilors)
    out = fv.faction_votes_from_named({"za": [0, 1], "przeciw": []}, resolve)
    assert out["SPD"]["za"] == 1 and out["CDU"]["za"] == 1


def test_resolver_from_councilors_bad_index():
    resolve = fv.resolver_from_councilors([{"club": "SPD"}])
    out = fv.faction_votes_from_named({"za": [99]}, resolve)
    assert out[fv.UNKNOWN_CLUB]["za"] == 1


# ── make_faction_vote (ścieżka francuska) ────────────────────────────

def test_make_faction_vote_counts_and_shape():
    vote = fv.make_faction_vote(
        "paris_2024_0001",
        "2024-03-15",
        "Budget primitif 2024",
        {
            "PARIS_EN_COMMUN": {"za": 90, "przeciw": 0, "wstrzymal_sie": 2},
            "LR": {"za": 0, "przeciw": 22, "wstrzymal_sie": 0},
        },
        club_seats={"PARIS_EN_COMMUN": 92, "LR": 22},
        source_url="https://example.fr/vote",
    )
    assert vote["vote_mode"] == "faction"
    assert vote["counts"]["za"] == 90
    assert vote["counts"]["przeciw"] == 22
    assert vote["counts"]["wstrzymal_sie"] == 2
    assert vote["faction_votes"]["PARIS_EN_COMMUN"]["seats"] == 92
    # named_votes obecne ale puste -> front uzna to za głosowanie frakcyjne
    assert all(vote["named_votes"][c] == [] for c in fv.CATEGORIES)


# ── faction_stance (lustrzane do frontu) ─────────────────────────────

def test_stance_clear_majority():
    assert fv.faction_stance({"za": 5, "przeciw": 1, "wstrzymal_sie": 0}) == "za"
    assert fv.faction_stance({"za": 0, "przeciw": 7}) == "przeciw"


def test_stance_tie_is_mixed():
    assert fv.faction_stance({"za": 3, "przeciw": 3}) == "mixed"


def test_stance_no_votes():
    # Grupa bez oddanych głosów (sami nieobecni / brak głosu) nie jest
    # "wstrzymaniem" — to brak udziału w głosowaniu.
    assert fv.faction_stance({"za": 0, "przeciw": 0, "wstrzymal_sie": 0}) == "none"
    assert fv.faction_stance({"brak_glosu": 3, "nieobecni": 2}) == "none"


# ── enrich_vote_with_factions ────────────────────────────────────────

def test_enrich_named_vote_keeps_named_mode():
    vote = {
        "named_votes": {"za": ["Anna"], "przeciw": ["Clara"]},
    }
    resolve = fv.resolver_from_name_map({"Anna": "CDU", "Clara": "SPD"})
    out = fv.enrich_vote_with_factions(vote, resolve)
    assert out["vote_mode"] == "named"
    assert out["faction_votes"]["CDU"]["za"] == 1


def test_enrich_force_faction_mode():
    vote = {"named_votes": {"za": ["Anna"]}}
    resolve = fv.resolver_from_name_map({"Anna": "CDU"})
    out = fv.enrich_vote_with_factions(vote, resolve, force_faction_mode=True)
    assert out["vote_mode"] == "faction"


def test_enrich_empty_named_becomes_faction():
    vote = {"named_votes": {c: [] for c in fv.CATEGORIES},
            "faction_votes": {"LR": {"przeciw": 5}}}
    out = fv.enrich_vote_with_factions(vote, lambda t: None)
    assert out["vote_mode"] == "faction"
    # istniejące faction_votes nie są nadpisywane
    assert out["faction_votes"]["LR"]["przeciw"] == 5


def test_is_faction_only():
    assert fv.is_faction_only({"voting_display": "faction"}) is True
    assert fv.is_faction_only({"voting_display": "named"}) is False
    assert fv.is_faction_only({}) is False


# ── i18n: rejestracja FR ─────────────────────────────────────────────

def test_fr_locale_registered():
    html = '<span>Głosowanie według frakcji</span>'
    out = i18n.apply_locale(html, "fr")
    assert "Vote par groupe" in out
    assert "Głosowanie według frakcji" not in out


def test_fr_faction_notice_translated():
    pl = ("W tym regionie głosowania imienne (per radny) nie są publicznie "
          "protokołowane. Pokazujemy wynik w rozbiciu na frakcje, zgodnie z "
          "oficjalnym protokołem.")
    out = i18n.apply_locale(f"<p>{pl}</p>", "fr")
    assert "votes nominaux" in out


def test_faction_stance_labels_translated():
    assert "Nicht abgestimmt" in i18n.apply_locale("none:'Nie głosowała'", "de")
    assert "N'a pas voté" in i18n.apply_locale("none:'Nie głosowała'", "fr")


def test_fr_vote_values():
    out = i18n.apply_locale("{za:'Za', przeciw:'Przeciw'}", "fr")
    assert "Pour" in out and "Contre" in out


def test_fr_does_not_touch_identifiers():
    # word-boundary regex nie powinien ruszyć identyfikatorów JS
    out = i18n.apply_locale("function renderInterpelacje(){}", "fr")
    assert "renderInterpelacje" in out


# ── Kontrakt front <-> helper (przez prawdziwe funkcje JS z template) ──

def _make_sample_faction_vote():
    return fv.make_faction_vote(
        "paris_2024_0001", "2024-03-15", "Budget primitif 2024",
        {
            "PARIS_EN_COMMUN": {"za": 90, "przeciw": 0, "wstrzymal_sie": 2},
            "LR": {"za": 0, "przeciw": 22, "wstrzymal_sie": 0},
            "CHANGER_PARIS": {"za": 5, "przeciw": 5},   # remis -> mixed
            "NON_INSCRITS": {"nieobecni": 3},           # sami nieobecni -> none
        },
        club_seats={"PARIS_EN_COMMUN": 92, "LR": 22,
                    "CHANGER_PARIS": 10, "NON_INSCRITS": 3},
    )


def test_frontend_contract_via_node():
    """Puść prawdziwe funkcje JS z template na wyjściu make_faction_vote().

    Łapie rozjazd kontraktu (nazwa kategorii, klasa stance, pole seats)
    między Pythonem a frontem. Skip, gdy w środowisku nie ma node.
    """
    import json
    import shutil
    import subprocess
    import tempfile

    if shutil.which("node") is None:
        import pytest
        pytest.skip("node niedostępny — pomijam test kontraktu frontu")

    here = os.path.dirname(__file__)
    harness = os.path.join(here, "verify_faction_frontend.mjs")
    assert os.path.exists(harness), "brak verify_faction_frontend.mjs"

    vote = _make_sample_faction_vote()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(vote, fh, ensure_ascii=False)
        vote_path = fh.name
    try:
        proc = subprocess.run(
            ["node", harness, vote_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.unlink(vote_path)

    assert proc.returncode == 0, f"harness JS nie przeszedl:\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    html = data["rendered"] + data["notice"]

    # Render musi zawierać wszystkie kody frakcji i nagłówek sekcji.
    for code in vote["faction_votes"]:
        assert code in html
    assert "Głosowanie według frakcji" in html

    # Lokalizacja: po tłumaczeniu żaden polski token UI nie zostaje.
    pl_tokens = ["Głosowanie według frakcji", "Wstrzymał się",
                 "Nie głosowała", "Podzielona", "mandat", "głosowania imienne"]
    for loc in ("de", "fr"):
        out = i18n.apply_locale(html, loc)
        leftover = [t for t in pl_tokens if t in out]
        assert not leftover, f"{loc}: nieprzetłumaczone {leftover}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
