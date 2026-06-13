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
    votes = [_vote(12, 11, topic="Plan zagospodarowania"), _vote(20, 0)]
    s = L.summarize_session({}, votes, None)
    f = L.best_session_fact({}, s)
    assert f["kind"] == "knife_edge"
    assert f["data"]["za"] == 12 and f["data"]["przeciw"] == 11


def test_knife_edge_term_record_bonus():
    votes = [_vote(12, 11, topic="Plan")]
    s = L.summarize_session({}, votes, None)
    base = L.best_session_fact({}, s)
    rec = L.best_session_fact({}, s, {"term_closest_margin": 1})
    assert rec["data"]["is_term_closest"] is True
    assert rec["score"] > base["score"]


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


def test_boring_session_fallback_none():
    votes = [_vote(20, 0), _vote(19, 0)]
    s = L.summarize_session({"attendee_count": 20}, votes, None)
    assert L.best_session_fact({}, s) is None


def test_results_pending_no_fact():
    votes = [_vote(12, 11)]
    s = L.summarize_session({"results_pending": True}, votes, None)
    assert L.rank_session_facts({}, s) == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"OK {fn.__name__}")
    print(f"\nALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
