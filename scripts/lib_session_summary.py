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
        "totals": totals,
        "attendee_count": attendee_count,
        "absent": absent,
        "results_pending": results_pending,
    }
