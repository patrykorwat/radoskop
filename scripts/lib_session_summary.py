#!/usr/bin/env python3
"""Wspólne liczenie podsumowania sesji rady.

Jedyne źródło heurystyki "głosowania spornego". Ta sama reguła żyje w JS
(template/index.html, landing: filtr contested) i musi się z nią zgadzać:
głosowanie jest sporne, gdy mniejsza ze stron (za/przeciw) ma niezerowy
wynik i stanowi co najmniej 1/3 sumy za+przeciw. To NIE jest "różnica < 10";
stary opis w landing_strings.py został poprawiony 2026-06-10.

Konsumenci:
  - generate_seo_pages.py  (prerender /session/{n}/ z podsumowaniem)
  - generate_og_images.py  (karta OG sesji)
  - generate_feed.py       (summary itemu sesji w feed/atom)
"""

from __future__ import annotations


def is_contested(counts: dict) -> bool:
    """Sporne: mniejszość (za vs przeciw) niezerowa i >= 1/3 sumy obu stron."""
    if not isinstance(counts, dict):
        return False
    za = counts.get("za", 0) or 0
    przeciw = counts.get("przeciw", 0) or 0
    m = min(za, przeciw)
    return m > 0 and m * 3 >= (za + przeciw)


def minority_share(counts: dict) -> float:
    """Udział mniejszości w sumie za+przeciw (0..0.5). Do sortowania spornych."""
    if not isinstance(counts, dict):
        return 0.0
    za = counts.get("za", 0) or 0
    przeciw = counts.get("przeciw", 0) or 0
    total = za + przeciw
    if total <= 0:
        return 0.0
    return min(za, przeciw) / total


def valid_session_number(snum) -> bool:
    """Czy number sesji nadaje się na segment URL / nazwę pliku.

    Ta sama reguła co guard w generate_seo_pages.py: krótki identyfikator
    (rzymski "XXIII", arabski "23", data ISO). Spacje, słowa typu "Sesja"
    albo długość > 30 znaków oznaczają zepsutą ekstrakcję ze scrape'a.
    """
    if snum is None:
        return False
    s = str(snum).strip()
    if not s or len(s) > 30 or " " in s:
        return False
    lower = s.lower()
    return not any(bad in lower for bad in ("sesja", "rada", "miast", "rady"))


def session_votes(session: dict, votes: list) -> list:
    """Głosowania należące do sesji.

    Preferowane dopasowanie po session_number (odporne na dwie sesje tego
    samego dnia, patrz bug Radomia x1024). Gdy głosowania nie niosą numeru
    (np. Warszawa), fallback na datę; sesje wielodniowe łapią wszystkie
    swoje daty przez dates_in_session.
    """
    if not votes:
        return []
    snum = str(session.get("number") or "").strip()
    dates = set()
    if session.get("date"):
        dates.add(session["date"])
    for d in session.get("dates_in_session") or []:
        if d:
            dates.add(d)

    if snum:
        by_number = [
            v for v in votes
            if str(v.get("session_number") or "").strip() == snum
        ]
        if by_number:
            return by_number
    if dates:
        return [v for v in votes if v.get("session_date") in dates]
    return []


def summarize_session(session: dict, votes: list, councilors: list | None = None) -> dict:
    """Policz podsumowanie jednej sesji.

    Zwraca dict z polami:
      vote_count, passed, rejected, unanimous, contested (lista głosowań,
      najbardziej sporne najpierw), contested_count, totals {za, przeciw,
      wstrzymal_sie}, attendee_count, absent (posortowana lista nazwisk),
      results_pending.

    Działa też dla miast faction-mode: counts są zagregowane per frakcja,
    więc cała arytmetyka za/przeciw przechodzi bez zmian; absent wyjdzie
    puste (brak attendees imiennych).
    """
    totals = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0}
    passed = rejected = unanimous = 0
    contested = []
    rejected_votes = []

    for v in votes or []:
        c = v.get("counts") or {}
        za = c.get("za", 0) or 0
        przeciw = c.get("przeciw", 0) or 0
        wstrzymal = c.get("wstrzymal_sie", 0) or 0
        totals["za"] += za
        totals["przeciw"] += przeciw
        totals["wstrzymal_sie"] += wstrzymal
        if za > przeciw:
            passed += 1
        elif przeciw > za:
            rejected += 1
            rejected_votes.append(v)
        if za > 0 and przeciw == 0 and wstrzymal == 0:
            unanimous += 1
        if is_contested(c):
            contested.append(v)

    contested.sort(key=lambda v: minority_share(v.get("counts") or {}), reverse=True)

    results_pending = bool(session.get("results_pending"))
    attendees = session.get("attendees") or []
    attendee_count = session.get("attendee_count") or (len(attendees) if attendees else 0)

    # Nieobecni tylko gdy mamy listę obecnych i wyniki są opublikowane.
    # Brak attendees nie znaczy, że wszyscy byli nieobecni (lekcja z fallbacku
    # results_pending w SPA).
    absent: list[str] = []
    if councilors and attendees and not results_pending:
        present = set(attendees)
        names = set()
        for c in councilors:
            if isinstance(c, dict):
                n = (c.get("name") or "").strip()
            else:
                n = str(c).strip()
            if n:
                names.add(n)
        absent = sorted(n for n in names if n not in present)

    return {
        "vote_count": len(votes or []) or (session.get("vote_count") or 0),
        "passed": passed,
        "rejected": rejected,
        "unanimous": unanimous,
        "contested": contested,
        "contested_count": len(contested),
        "rejected_votes": rejected_votes,
        "totals": totals,
        "attendee_count": attendee_count,
        "absent": absent,
        "results_pending": results_pending,
    }


# ── Wybór najmocniejszego faktu sesji (chwytliwa treść social) ───────────
# Cienka warstwa rankingu NA WIERZCHU summarize_session. Nic nie liczy od
# nowa poza scoringiem; wybiera najmocniejszy fakt z liczb, które już mamy
# pewne. Zero interpretacji/spinu, deterministyczne. Render osobno (x_bot).

def _abs_margin(counts: dict) -> int:
    za = counts.get("za", 0) or 0
    przeciw = counts.get("przeciw", 0) or 0
    return abs(za - przeciw)


def rank_session_facts(session: dict, summary: dict, context: dict | None = None) -> list[dict]:
    """Lista kandydatów-faktów sesji posortowana malejąco po `score`.

    `summary` = wynik summarize_session. `context` (opcjonalny) niesie rekordy
    kadencji policzone raz przez wołającego (generate_feed):
        term_closest_margin : najmniejszy abs(za-przeciw) wśród spornych w kadencji
        term_max_vote_count : najwięcej głosowań w jednej sesji w kadencji
    Każdy fakt: {kind, score, data}; `data` to surowe liczby do szablonu.
    Wyniki nieopublikowane => [] (caller robi changelog).
    """
    if summary.get("results_pending"):
        return []
    ctx = context or {}
    facts: list[dict] = []

    contested = summary.get("contested") or []
    absent = summary.get("absent") or []
    attendee_count = summary.get("attendee_count", 0) or 0
    vote_count = summary.get("vote_count", 0) or 0
    contested_count = summary.get("contested_count", 0) or 0

    # 1) Głosowanie na styk (knife-edge) — najmocniejszy hook.
    if contested:
        top = contested[0]
        c = top.get("counts") or {}
        share = minority_share(c)              # 0.33..0.5
        margin = _abs_margin(c)
        score = 60 + (share - 0.33) * 200      # ~60 przy 1/3, ~94 przy 50/50
        term_closest = ctx.get("term_closest_margin")
        is_record = term_closest is not None and margin <= term_closest
        if is_record:
            score += 25
        facts.append({"kind": "knife_edge", "score": round(score, 2), "data": {
            "topic": (top.get("topic") or "").replace(";", "").strip(),
            "za": c.get("za", 0) or 0, "przeciw": c.get("przeciw", 0) or 0,
            "wstrzymal_sie": c.get("wstrzymal_sie", 0) or 0,
            "margin": margin, "is_term_closest": is_record,
        }})

    # 2) Odrzucenie projektu — rzadsze niż przyjęcie, więc newsowe.
    decisive = [v for v in (summary.get("rejected_votes") or [])
                if (v.get("counts") or {}).get("przeciw", 0) > (v.get("counts") or {}).get("za", 0)]
    if decisive:
        r = max(decisive, key=lambda v: _abs_margin(v.get("counts") or {}))
        c = r.get("counts") or {}
        facts.append({"kind": "rejection", "score": 55, "data": {
            "topic": (r.get("topic") or "").replace(";", "").strip(),
            "za": c.get("za", 0) or 0, "przeciw": c.get("przeciw", 0) or 0,
        }})

    # 3) Anomalia frekwencji (tylko agregat, bez nazwisk).
    if attendee_count and absent:
        total_known = attendee_count + len(absent)
        share = len(absent) / total_known if total_known else 0.0
        if share >= 0.20:
            facts.append({"kind": "absence", "score": round(40 + share * 60, 2), "data": {
                "absent": len(absent), "total": total_known,
            }})

    # 4) Rekord aktywności: najwięcej głosowań w kadencji.
    term_max = ctx.get("term_max_vote_count")
    if term_max and vote_count and vote_count >= term_max:
        facts.append({"kind": "busiest", "score": 45, "data": {"vote_count": vote_count}})

    # 5) Dużo spornych naraz.
    if contested_count >= 3:
        facts.append({"kind": "contested_volume", "score": 35,
                      "data": {"contested_count": contested_count}})

    facts.sort(key=lambda f: f["score"], reverse=True)
    return facts


def best_session_fact(session: dict, summary: dict, context: dict | None = None,
                      floor: float = 35.0) -> dict | None:
    """Jeden najmocniejszy fakt powyżej progu, albo None => changelog."""
    ranked = rank_session_facts(session, summary, context)
    if ranked and ranked[0]["score"] >= floor:
        return ranked[0]
    return None
