#!/usr/bin/env python3
"""Wspólny model stenogramów sesji: tury wypowiedzi + statystyki dominacji.

Parsery miejskie (Kraków/Warszawa parse_stenogram.py, Gdańsk parse_protokoly.py,
Berlin scrape_sessions.py) wyciągały tekst wypowiedzi tylko po to, żeby policzyć
słowa, po czym treść była wyrzucana — zostawał agregat {name, statements, words}.
Ten moduł jest jednym źródłem przejścia z uporządkowanych TUR (pełna treść każdej
wypowiedzi w kolejności padania) na:

  * agregat mówców (zgodny wstecz z dotychczasowym `sess.speakers`),
  * statystyki dominacji ("kto przejął sesję"),
  * pełny obiekt stenogramu zapisywany per sesja (docs/transcripts/...),
  * skróty wypowiedzi danej osoby (zakładka Aktywność w profilu).

Tura (turn) na wejściu to dict:
    {"name": str, "text": str, "words": int (opcj.), "role": str (opcj.)}
`words` jest opcjonalne — gdy go brak, liczymy z `text`.

Konsumenci:
  - cities/*/scripts/*  (parsery emitujące tury → build_transcript)
  - scripts/build_metrics.py, build_profiles.py  (Gdańsk: activity z tur)
  - radoskop-premium/scripts/data_api.py  (serwowanie /session/<n>/transcript)
  - scripts/generate_seo_pages.py  (prerender strony stenogramu + zakładki)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(__file__))

from lib_session_summary import canonical_name  # noqa: E402  (jedno źródło kanonu nazwisk)

# Domyślna długość skrótu wypowiedzi (znaki) w zakładce Aktywność i kartach OG.
DEFAULT_EXCERPT = 260

# Minimalna liczba słów, żeby tura była "wypowiedzią" a nie szumem ("Dziękuję.").
# Parsery i tak filtrują, ale build_transcript broni się przed pustymi turami.
MIN_TURN_WORDS = 1


def count_words(text: str) -> int:
    """Policz słowa w tekście (spójnie z parserami miejskimi)."""
    return len((text or "").split())


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Przytnij tekst do max_chars na granicy słowa. Zwraca (skrót, czy_uciety)."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    # Cofnij się do ostatniej spacji, żeby nie ciąć słowa w połowie.
    sp = cut.rfind(" ")
    if sp > max_chars * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.–-") + "…", True


def _canon_club_lookup(club_lookup: dict | None) -> dict:
    """Zbuduj mapę kanoniczne_nazwisko → klub odporną na format nazwiska."""
    if not club_lookup:
        return {}
    out = {}
    for name, club in club_lookup.items():
        if not club:
            continue
        out[canonical_name(name)] = club
    return out


def normalize_turns(turns: Iterable[dict]) -> list[dict]:
    """Oczyść listę tur: policz brakujące `words`, odrzuć puste, przypisz seq.

    Nie zmienia kolejności — kolejność padania wypowiedzi jest danymi.
    """
    out: list[dict] = []
    seq = 0
    for t in turns or []:
        name = (t.get("name") or "").strip()
        text = (t.get("text") or "").strip()
        words = int(t.get("words") or 0) or count_words(text)
        if not name or words < MIN_TURN_WORDS:
            continue
        turn = {"seq": seq, "name": name, "text": text, "words": words}
        role = (t.get("role") or "").strip()
        if role:
            turn["role"] = role
        out.append(turn)
        seq += 1
    return out


def aggregate_speakers(turns: Iterable[dict], club_lookup: dict | None = None) -> list[dict]:
    """Zsumuj tury per mówca → [{name, statements, words, share, club?}].

    Zwraca posortowane malejąco po słowach (potem po liczbie wypowiedzi, potem
    po nazwisku). Kształt jest zgodny wstecz z dotychczasowym `sess.speakers`
    (name/statements/words); dokładamy `share` (udział w słowach 0..1) i `club`.
    """
    clubc = _canon_club_lookup(club_lookup)
    agg: dict[str, dict] = {}
    for t in turns or []:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        key = canonical_name(name)
        if key not in agg:
            agg[key] = {"name": name, "statements": 0, "words": 0}
        agg[key]["statements"] += 1
        agg[key]["words"] += int(t.get("words") or count_words(t.get("text", "")))

    total = sum(a["words"] for a in agg.values())
    out = []
    for key, a in agg.items():
        entry = {"name": a["name"], "statements": a["statements"], "words": a["words"]}
        entry["share"] = round(a["words"] / total, 4) if total else 0.0
        club = clubc.get(key)
        if club:
            entry["club"] = club
        out.append(entry)
    out.sort(key=lambda x: (-x["words"], -x["statements"], x["name"]))
    return out


def dominance_stats(turns: Iterable[dict]) -> dict:
    """Policz "kto przejął sesję" — panel wielometryczny.

    Liczone PO WSZYSTKICH mówcach (radni, przewodniczący, prezydent, urzędnicy),
    bo dominację w debacie potrafi przejąć rola spoza rady.

    Zwraca:
      total_words, total_statements, speaker_count
      top_speaker: {name, words, share}            — czołowy mówca
      top3_share:  udział słów trzech czołowych mówców (0..1)
      longest_monolog: {name, words, seq}          — najdłuższa pojedyncza tura
      concentration: HHI udziałów słów (0..1; 1 = monopol jednego mówcy)
    """
    turns = list(turns or [])
    speakers = aggregate_speakers(turns)
    total_words = sum(int(t.get("words") or count_words(t.get("text", ""))) for t in turns)
    total_statements = len(turns)

    top_speaker = None
    if speakers:
        s0 = speakers[0]
        top_speaker = {"name": s0["name"], "words": s0["words"], "share": s0.get("share", 0.0)}

    top3_words = sum(s["words"] for s in speakers[:3])
    top3_share = round(top3_words / total_words, 4) if total_words else 0.0

    longest_monolog = None
    if turns:
        lt = max(turns, key=lambda t: int(t.get("words") or count_words(t.get("text", ""))))
        longest_monolog = {
            "name": lt.get("name", ""),
            "words": int(lt.get("words") or count_words(lt.get("text", ""))),
            "seq": lt.get("seq"),
        }

    concentration = round(sum(s.get("share", 0.0) ** 2 for s in speakers), 4) if total_words else 0.0

    return {
        "total_words": total_words,
        "total_statements": total_statements,
        "speaker_count": len(speakers),
        "top_speaker": top_speaker,
        "top3_share": top3_share,
        "longest_monolog": longest_monolog,
        "concentration": concentration,
    }


def build_transcript(meta: dict, turns: Iterable[dict],
                     club_lookup: dict | None = None) -> dict:
    """Złóż pełny obiekt stenogramu do zapisu per sesja.

    meta: dowolne metadane sesji (city, kadencja, session_number, date,
          source_url, city_name...). Trafiają na wierzch wyniku.
    Wynik: meta + {turns: [...], speakers: [...], stats: {...}}.
    turns wynikowe mają: seq, name, words, text, (role?), (club?).
    """
    norm = normalize_turns(turns)
    clubc = _canon_club_lookup(club_lookup)
    for turn in norm:
        club = clubc.get(canonical_name(turn["name"]))
        if club:
            turn["club"] = club

    out = dict(meta or {})
    out["turns"] = norm
    out["speakers"] = aggregate_speakers(norm, club_lookup)
    out["stats"] = dominance_stats(norm)
    out["has_text"] = any(t.get("text") for t in norm)
    return out


def excerpts_for(turns: Iterable[dict], name: str,
                 max_chars: int = DEFAULT_EXCERPT,
                 max_items: int | None = None,
                 order: str = "words") -> list[dict]:
    """Skróty wypowiedzi danej osoby → [{seq, words, text, truncated}].

    Dopasowanie po kanonicznym nazwisku (odporne na format/ogonki/kolejność).
    order="words" sortuje od najdłuższej wypowiedzi (najtreściwsze na górze),
    order="seq" zachowuje kolejność z sesji. Skróty linkują do strony stenogramu
    przez seq (#turn-{seq}).
    """
    key = canonical_name(name)
    res = []
    for i, t in enumerate(turns or []):
        if canonical_name(t.get("name", "")) != key:
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        words = int(t.get("words") or count_words(text))
        snippet, truncated = _truncate(text, max_chars)
        res.append({
            "seq": t.get("seq", i),
            "words": words,
            "text": snippet,
            "truncated": truncated,
        })
    if order == "words":
        res.sort(key=lambda x: -x["words"])
    if max_items:
        res = res[:max_items]
    return res


# ── Zapis/odczyt plików stenogramów ──────────────────────────────────
# Układ: {docs_dir}/transcripts/{kadencja_id}/{safe_session_id}.json
# API serwuje to jako relatywny klucz "transcripts/{kid}/{id}.json" spod
# prefiksu docs miasta (S3), więc writer i reader muszą zgadzać się co do
# sanityzacji numeru sesji (rzymskie cyfry, daty, sufiksy są filesystem-safe).

TRANSCRIPTS_DIRNAME = "transcripts"


def safe_session_id(session_number) -> str:
    """Filesystem-safe identyfikator sesji do nazwy pliku stenogramu."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(session_number)).strip("_") or "session"


def transcript_rel_path(kadencja_id: str, session_number) -> str:
    """Relatywny klucz pliku stenogramu (spójny między writerem a API)."""
    return f"{TRANSCRIPTS_DIRNAME}/{kadencja_id}/{safe_session_id(session_number)}.json"


def write_transcript(docs_dir, kadencja_id: str, session_number, transcript: dict) -> str:
    """Zapisz stenogram do {docs_dir}/transcripts/{kid}/{id}.json. Zwraca ścieżkę."""
    rel = transcript_rel_path(kadencja_id, session_number)
    path = Path(docs_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False, separators=(",", ":"))
    return str(path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Użycie: python lib_stenogram.py <transcript.json|turns.json>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    turns = data.get("turns", data) if isinstance(data, dict) else data
    print(json.dumps(dominance_stats(turns), ensure_ascii=False, indent=2))
