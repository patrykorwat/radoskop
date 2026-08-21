"""Testy wyboru najmocniejszego faktu sesji (lib_session_summary).

Uruchom: python3 scripts/test_session_hooks.py   (z katalogu radoskop)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_session_summary as L


def _vote(za, przeciw, wstrzymal=0, topic="X"):
    return {"counts": {"za": za, "przeciw": przeciw, "wstrzymal_sie": wstrzymal}, "topic": topic}


def test_summarize_collects_rejected_votes():
    votes = [_vote(7, 20, topic="Apel"), _vote(15, 3)]
    s = L.summarize_session({}, votes, None)
    assert s["rejected"] == 1
    assert [v["topic"] for v in s["rejected_votes"]] == ["Apel"]


def test_knife_edge_wins():
    # różnica 2 głosów => zwykły knife_edge (margin 1 byłby one_vote)
    votes = [_vote(13, 11, topic="Plan zagospodarowania"), _vote(20, 0)]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "knife_edge"
    assert f["data"]["za"] == 13 and f["data"]["przeciw"] == 11


def test_knife_edge_term_record_bonus():
    votes = [_vote(13, 11, topic="Plan")]
    s = L.summarize_session({}, votes, None)
    base = L.best_session_fact({}, s)
    rec = L.best_session_fact({}, s, {"term_closest_margin": 2})
    assert rec["data"]["is_term_closest"] is True
    assert rec["score"] > base["score"]


def test_one_vote_margin_wins():
    # różnica jednego głosu => osobny, mocniejszy hook one_vote
    votes = [_vote(12, 11, topic="Plan zagospodarowania")]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "one_vote"
    assert f["data"]["margin"] == 1


def test_tie_deadlock_wins():
    # remis => hook tie, bije zwykły knife_edge
    votes = [_vote(11, 11, topic="Budżet"), _vote(13, 11)]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "tie"
    assert f["data"]["za"] == 11 and f["data"]["przeciw"] == 11


def test_tiny_tie_not_dramatized():
    # 1:1 (brak kworum) nie wchodzi w tie/one_vote — total_cast < 10
    votes = [_vote(1, 1, topic="Drobiazg")]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f is None or f["kind"] == "knife_edge"


def test_abstention_hook():
    # 8 wstrzymujących się przy 25 oddanych => hook abstention
    votes = [_vote(15, 2, wstrzymal=8, topic="Uchwała"), _vote(20, 0)]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "abstention"
    assert f["data"]["wstrzymal_sie"] == 8


def test_rejection_only():
    # 7:20 nie jest sporne (mniejszość <1/3), ale jest odrzuceniem -> rejection
    votes = [_vote(7, 20, topic="Wotum")]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "rejection" and f["data"]["przeciw"] == 20


def test_absence_when_no_votes_drama():
    councilors = [{"name": f"R{i}"} for i in range(25)]
    attendees = [f"R{i}" for i in range(18)]   # 7 nieobecnych / 25 = 28%
    session = {"attendees": attendees, "attendee_count": 18}
    votes = [_vote(20, 0), _vote(18, 0)]        # jednogłośne, brak dramy w głosach
    s = L.summarize_session(session, votes, councilors)
    f = L.best_session_fact(session, s)
    assert f["kind"] == "absence" and f["data"]["absent"] == 7


def test_canonical_name_order_and_diacritics_insensitive():
    assert L.canonical_name("Jan Kowalski") == L.canonical_name("KOWALSKI Jan")
    assert L.canonical_name("Łucja Żółć") == L.canonical_name("żółć łucja")
    assert L.canonical_name("") == ""


def test_absence_name_format_drift_still_matches():
    # Regresja Przemyśl: roster po swapie "Imię Nazwisko", obecni surowo
    # "NAZWISKO Imię". Stare porównanie surowych stringów dawało zero trafień
    # i absent = cały roster. Kanoniczny klucz musi je złożyć.
    councilors = [{"name": "Jan Kowalski"}, {"name": "Anna Nowak"}, {"name": "Piotr Lis"}]
    attendees = ["KOWALSKI Jan", "NOWAK Anna"]   # Lis nieobecny
    session = {"attendees": attendees, "attendee_count": 2}
    s = L.summarize_session(session, [_vote(2, 0)], councilors)
    assert s["absent"] == ["Piotr Lis"]


def test_absence_explicit_names_excludes_former_councillors():
    # absent_names z samej sesji (eSesja "nieobecni"). Były radny spoza rostera
    # nie może trafić do nieobecnych aktualnej sesji.
    councilors = [{"name": "Jan Kowalski"}, {"name": "Anna Nowak"}, {"name": "Piotr Lis"}]
    session = {"attendees": ["Jan Kowalski", "Anna Nowak"], "attendee_count": 2,
               "absent_names": ["Piotr Lis", "Były Radny"]}
    s = L.summarize_session(session, [_vote(2, 0)], councilors)
    assert s["absent"] == ["Piotr Lis"]


def test_absence_quorum_guard_suppresses_broken_match():
    # Dokładny scenariusz fałszywego tweeta: 22 obecnych, roster 28, zero
    # trafień (inny format) => absent 28, share 0.56. Sesja miała ważne
    # głosowanie, więc miała kworum: udział >= 0.5 jest niemożliwy => brak hooka.
    councilors = [{"name": f"R{i}"} for i in range(28)]
    attendees = [f"P{i}" for i in range(22)]
    session = {"attendees": attendees, "attendee_count": 22}
    s = L.summarize_session(session, [_vote(20, 2)], councilors)
    f = L.best_session_fact(session, s)
    assert f is None or f["kind"] != "absence"


def test_boring_session_fallback_none():
    votes = [_vote(20, 0), _vote(19, 0)]
    s = L.summarize_session({"attendee_count": 20}, votes, None)
    assert L.best_session_fact({}, s) is None


def test_results_pending_no_fact():
    votes = [_vote(12, 11)]
    s = L.summarize_session({"results_pending": True}, votes, None)
    assert L.rank_session_facts({}, s) == []


def test_no_roll_call_votes_no_absent():
    # Sesja bez głosowań imiennych nie może generować listy nieobecnych
    # (brak attendees != wszyscy nieobecni). Nawet gdy attendees są puste,
    # absent musi zostać puste i nie może być liczone z rosters.
    s = L.summarize_session({"no_roll_call_votes": True, "attendees": []}, [], ["Anna A", "Jan B"])
    assert s["no_roll_call_votes"] is True
    assert s["absent"] == []
    # results_pending traktowane niezależnie — obie flagi nie generują absent
    s2 = L.summarize_session({"no_roll_call_votes": True}, [], ["Anna A"])
    assert s2["absent"] == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"OK {fn.__name__}")
    print(f"\nALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
