"""
Radoskop — budowanie danych głosowań frakcyjnych.

Część regionów nie protokołuje głosów per radny. Francja głosuje domyślnie
"à main levée" (przez podniesienie ręki) i publikuje wynik tylko jako
"tableau des votes par groupe". Część niemieckich gremiów robi to samo —
namentliche Abstimmung jest wyjątkiem, większość to pokaz rąk z wynikiem
per Fraktion. W takich miastach nie da się zbudować named_votes (lista
nazwisk per kategoria), ale da się pokazać rozbicie na frakcje.

Frontend (template/index.html) renderuje takie głosowania, gdy vote ma:

    vote["vote_mode"]    == "faction"
    vote["faction_votes"] == {
        "<KOD_FRAKCJI>": {
            "za": int, "przeciw": int, "wstrzymal_sie": int,
            "brak_glosu": int, "nieobecni": int,
            "seats": int | None,   # opcjonalne: liczba mandatów grupy
        },
        ...
    }

Klucz frakcji musi pasować do kluczy w config["clubs"] (np. "CDU", "SPD"),
żeby clubColor() na froncie dobrał właściwy kolor. faction_votes można:

  1. zagregować z named_votes (gdy źródło ma jednak nazwiska — wtedy
     dostajemy widok frakcyjny "za darmo"), albo
  2. zbudować wprost z surowych liczników grup (źródła francuskie),
     funkcją make_faction_vote().

Ten moduł jest czystą biblioteką — żadnego I/O, żeby dało się go używać
z dowolnego scrapera oraz testować bez sieci.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional

# Kategorie w kolejności wyświetlania na pasku frakcji (zgodnie z frontem).
CATEGORIES: tuple[str, ...] = (
    "za",
    "przeciw",
    "wstrzymal_sie",
    "brak_glosu",
    "nieobecni",
)

# Kategorie liczone jako "oddany głos" (do wyznaczania stanowiska grupy).
STANCE_CATEGORIES: tuple[str, ...] = ("za", "przeciw", "wstrzymal_sie")

# Etykieta grupy bez przynależności klubowej (fraktionslos / non-inscrits).
UNKNOWN_CLUB = "NZ"


def _empty_tally() -> dict[str, int]:
    return {cat: 0 for cat in CATEGORIES}


def faction_votes_from_named(
    named_votes: Mapping[str, Iterable],
    resolve_club: Callable[[object], Optional[str]],
    club_seats: Optional[Mapping[str, int]] = None,
    unknown_club: str = UNKNOWN_CLUB,
) -> dict[str, dict]:
    """Zagreguj named_votes (per radny) do liczników per frakcja.

    named_votes: {kategoria: [token, ...]} gdzie token to nazwisko ALBO
        indeks radnego — cokolwiek przyjmuje resolve_club.
    resolve_club: token -> kod frakcji (np. nazwisko -> "CDU", albo indeks ->
        config po przeliczeniu). Zwrócenie None/"" wpada do `unknown_club`.
    club_seats: opcjonalna mapa kod_frakcji -> liczba mandatów; dopisywana
        jako "seats" do każdej grupy, która się pojawi.

    Zwraca słownik gotowy do podstawienia jako vote["faction_votes"].
    """
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        for token in named_votes.get(cat, []) or []:
            club = resolve_club(token) or unknown_club
            tally = out.setdefault(club, _empty_tally())
            tally[cat] += 1
    if club_seats:
        for club, tally in out.items():
            if club in club_seats:
                tally["seats"] = int(club_seats[club])
    return out


def make_faction_vote(
    vote_id: str,
    session_date: str,
    topic: str,
    faction_tallies: Mapping[str, Mapping[str, int]],
    *,
    club_seats: Optional[Mapping[str, int]] = None,
    session_number: Optional[str] = None,
    source_url: Optional[str] = None,
    druk: Optional[str] = None,
    resolution: Optional[str] = None,
    result: Optional[str] = None,
    extra: Optional[Mapping] = None,
) -> dict:
    """Zbuduj rekord głosowania frakcyjnego ze surowych liczników grup.

    Ścieżka dla źródeł typu francuskiego — bez nazwisk, tylko tableau par
    groupe. faction_tallies: {kod_frakcji: {"za":..,"przeciw":..,...}}.
    Brakujące kategorie uzupełniamy zerami. Zbiorcze `counts` liczone jest
    z sumy frakcji, więc front pokazuje spójny nagłówek wyniku.
    """
    faction_votes: dict[str, dict] = {}
    counts = _empty_tally()
    for club, raw in faction_tallies.items():
        tally = _empty_tally()
        for cat in CATEGORIES:
            tally[cat] = int(raw.get(cat, 0) or 0)
            counts[cat] += tally[cat]
        if club_seats and club in club_seats:
            tally["seats"] = int(club_seats[club])
        elif "seats" in raw and raw["seats"] is not None:
            tally["seats"] = int(raw["seats"])
        faction_votes[club] = tally

    vote: dict = {
        "id": vote_id,
        "session_date": session_date,
        "session_number": session_number,
        "source_url": source_url,
        "topic": topic,
        "druk": druk,
        "resolution": resolution,
        "result": result,
        "counts": counts,
        "named_votes": {cat: [] for cat in CATEGORIES},
        "faction_votes": faction_votes,
        "vote_mode": "faction",
        "voted_at": session_date,
    }
    if extra:
        vote.update(extra)
    return vote


def faction_stance(group: Mapping[str, int]) -> str:
    """Dominujący kierunek grupy: 'za' | 'przeciw' | 'wstrzymal_sie' | 'mixed' | 'none'.

    Logika lustrzana do factionStance() na froncie: stanowisko jest
    jednoznaczne tylko gdy jeden kierunek dominuje; remis → 'mixed'.
    Grupa bez oddanych głosów (sami nieobecni / brak głosu) → 'none',
    bo etykietowanie jej jako "Wstrzymał się" zniekształcałoby protokół.
    """
    za = int(group.get("za", 0) or 0)
    prz = int(group.get("przeciw", 0) or 0)
    wst = int(group.get("wstrzymal_sie", 0) or 0)
    top = max(za, prz, wst)
    if top == 0:
        return "none"
    if [za, prz, wst].count(top) > 1:
        return "mixed"
    if top == za:
        return "za"
    if top == prz:
        return "przeciw"
    return "wstrzymal_sie"


def enrich_vote_with_factions(
    vote: dict,
    resolve_club: Callable[[object], Optional[str]],
    *,
    club_seats: Optional[Mapping[str, int]] = None,
    force_faction_mode: bool = False,
    unknown_club: str = UNKNOWN_CLUB,
) -> dict:
    """Dołącz faction_votes do pojedynczego głosowania (mutuje i zwraca).

    - Jeśli vote już ma faction_votes → tylko upewnij się, że vote_mode jest
      ustawiony (gdy force_faction_mode lub brak named_votes).
    - W przeciwnym razie agreguj z named_votes.
    - force_faction_mode=True ustawia vote_mode="faction" (region bez głosowań
      imiennych — front pokaże notkę i widok frakcyjny zamiast listy nazwisk).
    """
    has_named = any((vote.get("named_votes") or {}).get(c) for c in CATEGORIES)

    if not vote.get("faction_votes"):
        nv = vote.get("named_votes") or {}
        vote["faction_votes"] = faction_votes_from_named(
            nv, resolve_club, club_seats=club_seats, unknown_club=unknown_club
        )

    if force_faction_mode or not has_named:
        vote["vote_mode"] = "faction"
    else:
        vote.setdefault("vote_mode", "named")
    return vote


def enrich_votes(
    votes: Iterable[dict],
    resolve_club: Callable[[object], Optional[str]],
    *,
    club_seats: Optional[Mapping[str, int]] = None,
    force_faction_mode: bool = False,
    unknown_club: str = UNKNOWN_CLUB,
) -> list[dict]:
    """enrich_vote_with_factions zastosowane do listy głosowań."""
    return [
        enrich_vote_with_factions(
            v,
            resolve_club,
            club_seats=club_seats,
            force_faction_mode=force_faction_mode,
            unknown_club=unknown_club,
        )
        for v in votes
    ]


def is_faction_only(config: Mapping) -> bool:
    """Czy miasto/gremium ma być traktowane jako region frakcyjny.

    Sterowane jawnym polem config["voting_display"] == "faction". Funkcja
    nie zgaduje po kraju — decyzja należy do konfiguracji miasta, bo część
    miast DE ma jednak namentliche Abstimmung (np. Landtag MV).
    """
    return str(config.get("voting_display", "")).lower() == "faction"


def resolver_from_name_map(
    name_to_club: Mapping[str, str],
) -> Callable[[object], Optional[str]]:
    """resolve_club dla named_votes trzymanych jako nazwiska."""
    return lambda token: name_to_club.get(token)


def resolver_from_councilors(
    councilors: list[Mapping],
    club_key: str = "club",
) -> Callable[[object], Optional[str]]:
    """resolve_club dla named_votes trzymanych jako indeksy do listy radnych.

    councilors[idx][club_key] to kod frakcji. Tolerancyjny na zły indeks.
    """
    def _resolve(token: object) -> Optional[str]:
        try:
            return councilors[int(token)].get(club_key)
        except (ValueError, TypeError, IndexError, AttributeError):
            return None

    return _resolve
