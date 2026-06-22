"""Testy wspólnego modelu stenogramów lib_stenogram."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import lib_stenogram as st  # noqa: E402


def _turns():
    return [
        {"name": "Jan Kowalski", "text": "słowo " * 100, "role": "Przewodniczący"},
        {"name": "Anna Nowak", "text": "tekst " * 40},
        {"name": "Jan Kowalski", "text": "kolejna " * 60},
        {"name": "Piotr Zięba", "text": "krótko " * 10},
        {"name": "anna nowak", "text": "jeszcze " * 20},  # inny format → ten sam mówca
    ]


# ── normalize_turns ──────────────────────────────────────────────────

def test_normalize_assigns_seq_and_counts_words():
    turns = st.normalize_turns([
        {"name": "A", "text": "raz dwa trzy"},
        {"name": "B", "text": "", "words": 0},          # pusta → odrzucona
        {"name": "", "text": "bez nazwiska"},            # brak nazwiska → odrzucona
        {"name": "C", "text": "cztery pięć", "words": 99},  # words podane wprost
    ])
    assert [t["seq"] for t in turns] == [0, 1]
    assert turns[0]["name"] == "A" and turns[0]["words"] == 3
    assert turns[1]["name"] == "C" and turns[1]["words"] == 99


def test_normalize_keeps_role():
    turns = st.normalize_turns([{"name": "A", "text": "x y", "role": "Radny"}])
    assert turns[0]["role"] == "Radny"


# ── aggregate_speakers ───────────────────────────────────────────────

def test_aggregate_merges_name_variants_and_sorts_by_words():
    sp = st.aggregate_speakers(_turns())
    # anna nowak + Anna Nowak złączone
    names = [s["name"] for s in sp]
    assert names.count("Anna Nowak") + names.count("anna nowak") == 1
    # Jan Kowalski najwięcej słów (100+60) → pierwszy
    assert sp[0]["name"] == "Jan Kowalski"
    assert sp[0]["statements"] == 2
    assert sp[0]["words"] == 160
    # share sumuje się do ~1 (zaokrąglone do 4 miejsc, drobny błąd dopuszczalny)
    assert abs(sum(s["share"] for s in sp) - 1.0) < 1e-3


def test_aggregate_attaches_club_by_canonical_name():
    sp = st.aggregate_speakers(_turns(), club_lookup={"Jan Kowalski": "KO", "anna nowak": "PiS"})
    by = {s["name"]: s for s in sp}
    assert by["Jan Kowalski"]["club"] == "KO"
    # klub przypięty mimo różnicy wielkości liter w lookupie
    assert any(s.get("club") == "PiS" for s in sp)


# ── dominance_stats ──────────────────────────────────────────────────

def test_dominance_top_speaker_and_longest_monolog():
    d = st.dominance_stats(st.normalize_turns(_turns()))
    assert d["speaker_count"] == 3
    assert d["top_speaker"]["name"] == "Jan Kowalski"
    assert 0 < d["top_speaker"]["share"] <= 1
    # najdłuższy monolog to pierwsza tura Jana (100 słów), seq 0
    assert d["longest_monolog"]["words"] == 100
    assert d["longest_monolog"]["seq"] == 0
    # top3_share = 1.0 bo dokładnie 3 mówców
    assert abs(d["top3_share"] - 1.0) < 1e-6


def test_dominance_concentration_monopoly_vs_split():
    mono = st.dominance_stats([{"name": "A", "text": "x " * 100}])
    assert abs(mono["concentration"] - 1.0) < 1e-6
    split = st.dominance_stats([
        {"name": "A", "text": "x " * 50},
        {"name": "B", "text": "y " * 50},
    ])
    assert abs(split["concentration"] - 0.5) < 1e-6


def test_dominance_empty():
    d = st.dominance_stats([])
    assert d["total_words"] == 0 and d["speaker_count"] == 0
    assert d["top_speaker"] is None and d["longest_monolog"] is None


# ── build_transcript ─────────────────────────────────────────────────

def test_build_transcript_shape():
    meta = {"city": "krakow", "kadencja": "2024-2029", "session_number": "XLVI",
            "date": "2026-02-25", "source_url": "http://x"}
    tr = st.build_transcript(meta, _turns(), club_lookup={"Jan Kowalski": "KO"})
    assert tr["city"] == "krakow" and tr["session_number"] == "XLVI"
    assert len(tr["turns"]) == 5
    assert tr["turns"][0]["seq"] == 0
    assert tr["turns"][0].get("club") == "KO"
    assert "stats" in tr and "speakers" in tr
    assert tr["has_text"] is True


# ── excerpts_for ─────────────────────────────────────────────────────

def test_excerpts_for_person_matches_variants_and_truncates():
    long_text = "zdanie testowe numer kolejne " * 30  # > DEFAULT_EXCERPT znaków
    turns = st.normalize_turns([
        {"name": "Jan Kowalski", "text": long_text},
        {"name": "Anna Nowak", "text": "nie ten mówca"},
        {"name": "jan kowalski", "text": "druga wypowiedź jana"},
    ])
    ex = st.excerpts_for(turns, "Jan Kowalski")
    assert len(ex) == 2  # obie tury Jana mimo różnicy wielkości liter
    assert ex[0]["truncated"] is True and ex[0]["text"].endswith("…")
    assert all("seq" in e for e in ex)


def test_excerpts_respects_max_items_and_order():
    turns = st.normalize_turns([
        {"name": "A", "text": "x " * 5},
        {"name": "A", "text": "y " * 50},
        {"name": "A", "text": "z " * 20},
    ])
    ex = st.excerpts_for(turns, "A", max_items=2, order="words")
    assert len(ex) == 2
    assert ex[0]["words"] == 50  # najdłuższa pierwsza


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
